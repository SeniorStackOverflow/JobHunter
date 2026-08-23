from pathlib import Path


def test_admin_javascript_uses_in_app_confirmation_dialog() -> None:
    script = Path("app/admin/static/admin.js").read_text(encoding="utf-8")

    assert "window.confirm" not in script
    assert "window.alert" not in script
    assert ".showModal()" in script
    assert "data-confirm-reason" in script
    assert "dialog.returnValue = '';" in script
