from monitor import (
    Fixture,
    MatchDiffResult,
    PlayerEntry,
    TeamDiff,
    TeamLineup,
    calculate_team_diff,
    format_alert,
    normalize_player_name,
    build_parser,
    is_noise_player_name,
    is_noise_team_name,
)


def test_normalize_player_name() -> None:
    assert normalize_player_name("  Ivan   Petrov ") == "ivan petrov"
    assert normalize_player_name("ИВАН-ПЕТРОВ!!!") == "иванпетров"


def test_calculate_team_diff() -> None:
    prev = TeamLineup(
        team_name="Team A",
        starters=[PlayerEntry(name=f"P{i}") for i in range(1, 12)],
    )
    cur = TeamLineup(
        team_name="Team A",
        starters=[PlayerEntry(name=f"P{i}") for i in range(1, 9)]
        + [PlayerEntry(name="N1"), PlayerEntry(name="N2"), PlayerEntry(name="N3")],
    )
    diff = calculate_team_diff(cur, prev)
    assert diff.changes_count == 3
    assert {p.name for p in diff.left_out} == {"P9", "P10", "P11"}
    assert {p.name for p in diff.joined} == {"N1", "N2", "N3"}


def test_format_alert() -> None:
    result = MatchDiffResult(
        fixture=Fixture(
            home="A",
            away="B",
            starts_at_utc=__import__("datetime").datetime.now(__import__("datetime").UTC),
            match_url="https://example.com/current",
        ),
        previous_match_url="https://example.com/prev",
        home_diff=TeamDiff(
            team="A",
            changes_count=3,
            left_out=[PlayerEntry(name="Old", href="https://example.com/old")],
            joined=[PlayerEntry(name="New", href="https://example.com/new")],
        ),
        away_diff=TeamDiff(team="B", changes_count=0, left_out=[], joined=[]),
    )
    text = format_alert(result)
    assert "https://example.com/current" in text
    assert "https://example.com/prev" in text
    assert "<a href=\"https://example.com/old\">Old</a>" in text
    assert "Changes: 3" in text


def test_headless_flag_accepts_explicit_false() -> None:
    parser = build_parser()
    args = parser.parse_args(["--dry-run", "--headless", "false"])
    assert args.headless is False


def test_headless_flag_accepts_no_value() -> None:
    parser = build_parser()
    args = parser.parse_args(["--dry-run", "--headless"])
    assert args.headless is True


def test_browser_channel_argument_is_supported() -> None:
    parser = build_parser()
    args = parser.parse_args(["--dry-run", "--browser-channel", "chrome"])
    assert args.browser_channel == "chrome"


def test_noise_detection_filters_navigation_labels() -> None:
    assert is_noise_team_name("Media")
    assert is_noise_player_name("Fixtures")
    assert not is_noise_player_name("Ivan Petrov")
