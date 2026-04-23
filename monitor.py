#!/usr/bin/env python3
"""Football NSW lineups monitoring utility.

This script monitors upcoming fixtures on competitions.footballnsw.com.au,
waits for starting lineups publication and alerts about major lineup changes
(3+ differences) against each team's previous played match.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import html
import logging
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import aiohttp
from playwright.async_api import Browser, Page, async_playwright

DEFAULT_FIXTURES_URL = (
    "https://competitions.footballnsw.com.au/fixtures/"
    "?date_range=default&season=wOmelzGd02&competition=k2KpRRVWmY"
    "&league=k2KpDkrOKY&timezone=Europe%2FMoscow"
)

CLOUDFLARE_MARKERS = (
    "attention required",
    "cloudflare",
    "checking your browser",
    "just a moment",
)


@dataclasses.dataclass(slots=True)
class Config:
    fixtures_url: str
    timezone_offset_minutes: int
    limit: int
    headless: bool
    dry_run: bool
    poll_interval_seconds: int
    prestart_minutes: int
    post_start_grace_minutes: int
    telegram_token: str | None
    telegram_chat_id: str | None


@dataclasses.dataclass(slots=True)
class PlayerEntry:
    name: str
    href: str | None = None


@dataclasses.dataclass(slots=True)
class TeamLineup:
    team_name: str
    starters: list[PlayerEntry]


@dataclasses.dataclass(slots=True)
class Fixture:
    home: str
    away: str
    starts_at_utc: datetime
    match_url: str


@dataclasses.dataclass(slots=True)
class TeamDiff:
    team: str
    changes_count: int
    left_out: list[PlayerEntry]
    joined: list[PlayerEntry]


@dataclasses.dataclass(slots=True)
class MatchDiffResult:
    fixture: Fixture
    previous_match_url: str
    home_diff: TeamDiff
    away_diff: TeamDiff


class CloudflareDetectedError(RuntimeError):
    """Raised when Cloudflare challenge page is detected."""


class Monitor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.logger = logging.getLogger("monitor")

    async def run(self) -> None:
        self.logger.info("Starting monitor. headless=%s dry_run=%s", self.config.headless, self.config.dry_run)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.config.headless)
            try:
                page = await browser.new_page()
                fixtures = await self.fetch_upcoming_fixtures(page)
                self.logger.info("Upcoming fixtures found: %s", len(fixtures))
                for fixture in fixtures:
                    await self.monitor_fixture(browser, fixture)
            finally:
                await browser.close()

    async def fetch_upcoming_fixtures(self, page: Page) -> list[Fixture]:
        await navigate_and_check_cloudflare(page, self.config.fixtures_url)
        now = datetime.now(UTC)

        raw_matches = await extract_fixture_candidates(page)
        fixtures: list[Fixture] = []
        for candidate in raw_matches:
            parsed = parse_fixture_candidate(
                candidate,
                now=now,
                timezone_offset_minutes=self.config.timezone_offset_minutes,
                base_url=self.config.fixtures_url,
            )
            if parsed is None:
                self.logger.debug("Skipping candidate: %s", candidate)
                continue
            if parsed.starts_at_utc <= now:
                self.logger.debug("Skipping already started fixture: %s vs %s", parsed.home, parsed.away)
                continue
            fixtures.append(parsed)

        fixtures.sort(key=lambda f: f.starts_at_utc)
        limited = fixtures[: self.config.limit]
        if not limited:
            self.logger.warning(
                "No upcoming fixtures parsed. This may indicate page markup changes or Cloudflare challenge."
            )
        for fx in limited:
            self.logger.info("Selected for monitoring: %s vs %s at %s (%s)", fx.home, fx.away, fx.starts_at_utc, fx.match_url)
        return limited

    async def monitor_fixture(self, browser: Browser, fixture: Fixture) -> None:
        now = datetime.now(UTC)
        prestart_at = fixture.starts_at_utc - timedelta(minutes=self.config.prestart_minutes)

        if now < prestart_at:
            wait_seconds = (prestart_at - now).total_seconds()
            self.logger.info(
                "Fixture %s vs %s: monitoring starts at %s, sleeping %.0fs",
                fixture.home,
                fixture.away,
                prestart_at,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

        deadline = fixture.starts_at_utc + timedelta(minutes=self.config.post_start_grace_minutes)
        self.logger.info(
            "Monitoring lineups for %s vs %s every %ss until %s",
            fixture.home,
            fixture.away,
            self.config.poll_interval_seconds,
            deadline,
        )

        page = await browser.new_page()
        try:
            lineups: tuple[TeamLineup, TeamLineup] | None = None
            while datetime.now(UTC) <= deadline:
                try:
                    await navigate_and_check_cloudflare(page, fixture.match_url)
                except CloudflareDetectedError as exc:
                    self.logger.warning(
                        "Cloudflare challenge on match page (%s). In headed mode complete challenge manually. %s",
                        fixture.match_url,
                        exc,
                    )
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue

                lineups = await parse_lineups(page, fixture.match_url)
                if lineups is not None:
                    break
                self.logger.info("Lineups not ready yet for %s vs %s", fixture.home, fixture.away)
                await asyncio.sleep(self.config.poll_interval_seconds)

            if lineups is None:
                self.logger.warning(
                    "Lineups did not appear before deadline for %s vs %s, stopping monitoring.",
                    fixture.home,
                    fixture.away,
                )
                return

            self.logger.info("Lineups found for %s vs %s", fixture.home, fixture.away)
            diff_result = await self.compare_with_previous_match(browser, fixture, lineups)
            if diff_result is None:
                self.logger.info("Comparison skipped for %s vs %s due to missing historical data", fixture.home, fixture.away)
                return

            total_changes = diff_result.home_diff.changes_count + diff_result.away_diff.changes_count
            self.logger.info(
                "Diff computed. Home changes=%s Away changes=%s",
                diff_result.home_diff.changes_count,
                diff_result.away_diff.changes_count,
            )
            if (
                diff_result.home_diff.changes_count >= 3
                or diff_result.away_diff.changes_count >= 3
            ):
                if self.config.dry_run:
                    self.logger.info("Dry-run enabled: alert not sent. total_changes=%s", total_changes)
                else:
                    await send_telegram_alert(self.config, format_alert(diff_result))
                    self.logger.info("Telegram alert sent for %s vs %s", fixture.home, fixture.away)
            else:
                self.logger.info("No alert: both teams have less than 3 changes")
        finally:
            await page.close()

    async def compare_with_previous_match(
        self,
        browser: Browser,
        fixture: Fixture,
        current_lineups: tuple[TeamLineup, TeamLineup],
    ) -> MatchDiffResult | None:
        prev_url = await find_previous_match_url(browser, fixture)
        if prev_url is None:
            return None

        page = await browser.new_page()
        try:
            await navigate_and_check_cloudflare(page, prev_url)
            previous_lineups = await parse_lineups(page, prev_url)
            if previous_lineups is None:
                self.logger.warning("Previous match lineups unavailable: %s", prev_url)
                return None
        finally:
            await page.close()

        current_home, current_away = current_lineups
        prev_home, prev_away = previous_lineups

        home_diff = calculate_team_diff(current_home, prev_home)
        away_diff = calculate_team_diff(current_away, prev_away)
        return MatchDiffResult(
            fixture=fixture,
            previous_match_url=prev_url,
            home_diff=home_diff,
            away_diff=away_diff,
        )


async def navigate_and_check_cloudflare(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await asyncio.sleep(1.0)
    title = (await page.title()).strip().lower()
    body_text = (await page.inner_text("body")).strip().lower()
    haystack = f"{title}\n{body_text[:1000]}"

    if any(marker in haystack for marker in CLOUDFLARE_MARKERS):
        raise CloudflareDetectedError(
            "Cloudflare challenge detected by title/content markers. "
            "Run with headed mode (--headless=false, default) and pass check manually."
        )


async def extract_fixture_candidates(page: Page) -> list[dict[str, str]]:
    return await page.evaluate(
        """
        () => {
          const rows = Array.from(document.querySelectorAll('a, article, li, div'));
          const out = [];
          for (const node of rows) {
            const text = (node.textContent || '').trim();
            if (!text || text.length < 10) continue;
            if (!/\bvs\b|\bv\b|[-–—]/i.test(text)) continue;
            const href = node.closest('a')?.href || node.querySelector('a')?.href || '';
            const datetimeAttr = node.getAttribute('datetime') || node.querySelector('time')?.getAttribute('datetime') || '';
            const timeText = node.querySelector('time')?.textContent || '';
            out.push({ text, href, datetime: datetimeAttr, timeText });
          }
          return out.slice(0, 400);
        }
        """
    )


def parse_fixture_candidate(
    candidate: dict[str, str],
    now: datetime,
    timezone_offset_minutes: int,
    base_url: str,
) -> Fixture | None:
    text = re.sub(r"\s+", " ", candidate.get("text", "")).strip()
    if not text:
        return None

    pair = re.search(r"([A-Za-z0-9 .&'\-/]+?)\s+(?:vs|v|[-–—])\s+([A-Za-z0-9 .&'\-/]+)", text, re.IGNORECASE)
    if not pair:
        return None
    home = pair.group(1).strip(" -")
    away = pair.group(2).strip(" -")

    starts_at_utc: datetime | None = None
    dt_attr = candidate.get("datetime", "").strip()
    if dt_attr:
        starts_at_utc = parse_datetime(dt_attr, timezone_offset_minutes)

    if starts_at_utc is None:
        starts_at_utc = parse_datetime(candidate.get("timeText", ""), timezone_offset_minutes, reference=now)

    if starts_at_utc is None:
        full = candidate.get("text", "")
        parsed_in_text = parse_datetime(full, timezone_offset_minutes, reference=now)
        starts_at_utc = parsed_in_text

    href = candidate.get("href") or ""
    match_url = urljoin(base_url, href) if href else base_url

    if starts_at_utc is None:
        return None

    return Fixture(home=home, away=away, starts_at_utc=starts_at_utc, match_url=match_url)


def parse_datetime(raw: str, timezone_offset_minutes: int, reference: datetime | None = None) -> datetime | None:
    s = raw.strip()
    if not s:
        return None

    try:
        iso = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if iso.tzinfo is None:
            iso = iso.replace(tzinfo=UTC)
        return iso.astimezone(UTC)
    except ValueError:
        pass

    ref = reference or datetime.now(UTC)
    date_match = re.search(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", s)
    time_match = re.search(r"(\d{1,2}):(\d{2})", s)
    if not time_match:
        return None

    hour, minute = int(time_match.group(1)), int(time_match.group(2))
    if date_match:
        day, month = int(date_match.group(1)), int(date_match.group(2))
        year_raw = date_match.group(3)
        year = int(year_raw) if year_raw else ref.year
        if year < 100:
            year += 2000
    else:
        day, month, year = ref.day, ref.month, ref.year

    try:
        local_dt = datetime(year, month, day, hour, minute)
    except ValueError:
        return None

    utc_dt = local_dt - timedelta(minutes=timezone_offset_minutes)
    return utc_dt.replace(tzinfo=UTC)


async def parse_lineups(page: Page, source_url: str) -> tuple[TeamLineup, TeamLineup] | None:
    payload = await page.evaluate(
        r"""
        () => {
          const sections = Array.from(document.querySelectorAll('section, div, article'));
          const likely = [];
          for (const section of sections) {
            const text = (section.textContent || '').toLowerCase();
            if (!text.includes('lineup') && !text.includes('starting') && !text.includes('xi')) continue;
            likely.push(section);
          }
          const scope = likely[0] || document.body;
          const teamBlocks = Array.from(scope.querySelectorAll('[class*=team], [data-testid*=team], article, section, div'));
          const teams = [];
          for (const block of teamBlocks) {
            const teamName = (block.querySelector('h1,h2,h3,h4,.team-name,[class*=name]')?.textContent || '').trim();
            const players = [];
            for (const p of block.querySelectorAll('a, li, span, div')) {
              const t = (p.textContent || '').trim();
              if (!t || t.length < 2 || t.length > 40) continue;
              if (/coach|manager|substitute|bench|referee|stadium/i.test(t)) continue;
              if (!/[A-Za-z]/.test(t)) continue;
              if (/\d{1,2}:\d{2}/.test(t)) continue;
              const href = p.tagName.toLowerCase() === 'a' ? p.href : p.querySelector('a')?.href || '';
              players.push({ name: t, href });
            }
            if (teamName && players.length >= 11) {
              teams.push({ teamName, players });
            }
          }
          return teams.slice(0, 4);
        }
        """
    )

    parsed_teams: list[TeamLineup] = []
    for team in payload:
        unique = deduplicate_players(team.get("players", []))
        starters = [PlayerEntry(name=p["name"], href=p.get("href") or None) for p in unique[:11]]
        if len(starters) >= 11:
            parsed_teams.append(TeamLineup(team_name=team.get("teamName", "Unknown"), starters=starters))

    if len(parsed_teams) >= 2:
        logging.getLogger("monitor").debug(
            "Parsed lineups from %s: %s (%s), %s (%s)",
            source_url,
            parsed_teams[0].team_name,
            len(parsed_teams[0].starters),
            parsed_teams[1].team_name,
            len(parsed_teams[1].starters),
        )
        return parsed_teams[0], parsed_teams[1]

    logging.getLogger("monitor").debug("Failed to parse complete 11+11 lineups from %s", source_url)
    return None


async def find_previous_match_url(browser: Browser, fixture: Fixture) -> str | None:
    page = await browser.new_page()
    try:
        await navigate_and_check_cloudflare(page, fixture.match_url)
        href = await page.evaluate(
            """
            () => {
              const links = Array.from(document.querySelectorAll('a'));
              const candidate = links.find((a) => /previous|last match|results/i.test(a.textContent || ''));
              return candidate ? candidate.href : '';
            }
            """
        )
        if href:
            return href
        return None
    finally:
        await page.close()


def deduplicate_players(players: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for entry in players:
        name = normalize_player_name(entry.get("name", ""))
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": entry.get("name", "").strip(), "href": entry.get("href", "")})
    return out


def normalize_player_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip().lower()
    cleaned = re.sub(r"[^a-zа-яё0-9 ]+", "", cleaned)
    return cleaned


def calculate_team_diff(current: TeamLineup, previous: TeamLineup) -> TeamDiff:
    current_map = {normalize_player_name(p.name): p for p in current.starters}
    prev_map = {normalize_player_name(p.name): p for p in previous.starters}

    left_keys = [k for k in prev_map if k and k not in current_map]
    joined_keys = [k for k in current_map if k and k not in prev_map]

    return TeamDiff(
        team=current.team_name,
        changes_count=max(len(left_keys), len(joined_keys)),
        left_out=[prev_map[k] for k in left_keys],
        joined=[current_map[k] for k in joined_keys],
    )


def format_player(player: PlayerEntry) -> str:
    safe_name = html.escape(player.name)
    if player.href:
        return f'<a href="{html.escape(player.href)}">{safe_name}</a>'
    return safe_name


def format_alert(result: MatchDiffResult) -> str:
    fixture = result.fixture
    lines = [
        "<b>Lineup change alert (3+)</b>",
        f"Current match: <a href=\"{html.escape(fixture.match_url)}\">{html.escape(fixture.home)} vs {html.escape(fixture.away)}</a>",
        f"Previous match: <a href=\"{html.escape(result.previous_match_url)}\">link</a>",
        "",
        render_team_diff(result.home_diff),
        "",
        render_team_diff(result.away_diff),
    ]
    return "\n".join(lines)


def render_team_diff(diff: TeamDiff) -> str:
    left = ", ".join(format_player(p) for p in diff.left_out) if diff.left_out else "none"
    joined = ", ".join(format_player(p) for p in diff.joined) if diff.joined else "none"
    return (
        f"<b>{html.escape(diff.team)}</b>\n"
        f"Changes: {diff.changes_count}\n"
        f"Left out: {left}\n"
        f"Joined: {joined}"
    )


async def send_telegram_alert(config: Config, text: str) -> None:
    if not config.telegram_token or not config.telegram_chat_id:
        raise RuntimeError("Telegram credentials are required unless --dry-run is used")

    url = f"https://api.telegram.org/bot{config.telegram_token}/sendMessage"
    payload = {
        "chat_id": config.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=30) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Telegram API error {resp.status}: {body}")


def parse_bool_arg(value: str | bool) -> bool:
    """Parse common CLI boolean spellings."""
    if isinstance(value, bool):
        return value
    lowered = value.strip().lower()
    if lowered in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor Football NSW match lineups and notify Telegram.")
    parser.add_argument("--fixtures-url", default=DEFAULT_FIXTURES_URL)
    parser.add_argument("--timezone-offset-minutes", type=int, default=180, help="Minutes to subtract from local fixture time to UTC.")
    parser.add_argument("--limit", type=int, default=3, help="Max upcoming fixtures to monitor.")
    parser.add_argument(
        "--headless",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool_arg,
        help="Run browser in headless mode. Supports --headless, --headless true/false.",
    )
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Force headed mode.")
    parser.add_argument("--dry-run", action="store_true", help="No telegram sending, diagnostic mode.")
    parser.add_argument("--poll-interval-seconds", type=int, default=60)
    parser.add_argument("--prestart-minutes", type=int, default=60)
    parser.add_argument("--post-start-grace-minutes", type=int, default=5)
    parser.add_argument("--telegram-token", default=os.getenv("TELEGRAM_BOT_TOKEN"))
    parser.add_argument("--telegram-chat-id", default=os.getenv("TELEGRAM_CHAT_ID"))
    parser.add_argument("--log-level", default="INFO")
    return parser


def args_to_config(args: argparse.Namespace) -> Config:
    if not args.dry_run and (not args.telegram_token or not args.telegram_chat_id):
        raise SystemExit(
            "Telegram token/chat_id missing. Provide TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
            "or use --dry-run for diagnostics."
        )
    return Config(
        fixtures_url=args.fixtures_url,
        timezone_offset_minutes=args.timezone_offset_minutes,
        limit=args.limit,
        headless=args.headless,
        dry_run=args.dry_run,
        poll_interval_seconds=args.poll_interval_seconds,
        prestart_minutes=args.prestart_minutes,
        post_start_grace_minutes=args.post_start_grace_minutes,
        telegram_token=args.telegram_token,
        telegram_chat_id=args.telegram_chat_id,
    )


async def async_main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = args_to_config(args)
    monitor = Monitor(config)
    try:
        await monitor.run()
    except CloudflareDetectedError as exc:
        logging.getLogger("monitor").error(
            "Cloudflare page detected when loading fixtures. Run in headed mode and solve challenge manually. %s",
            exc,
        )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
