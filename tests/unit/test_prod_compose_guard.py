from pathlib import Path


def test_prod_compose_wrappers_require_canonical_root() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for name in ("prod-compose.sh", "prod-phone-compose.sh", "prod-browser-compose.sh"):
        text = (repo_root / "deploy" / name).read_text()
        assert '$(pwd -P)" != "/srv/jobhunter-prod"' in text
        assert "Production compose may only run from /srv/jobhunter-prod" in text
        assert "exit 64" in text
