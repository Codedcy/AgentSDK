from pathlib import Path


ROOT = Path(__file__).parents[2]
MIGRATION_PATTERN = "src/agent_sdk/storage/migrations/*.sql text eol=lf"


def test_repository_pins_packaged_migrations_to_lf() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert MIGRATION_PATTERN in attributes.splitlines()


def test_packaged_migrations_are_lf_in_checkout() -> None:
    migrations = sorted(
        (ROOT / "src/agent_sdk/storage/migrations").glob("*.sql")
    )

    assert migrations
    assert all(b"\r\n" not in migration.read_bytes() for migration in migrations)
