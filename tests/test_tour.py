"""The guided tour is offered on every page and its steps point at pages that exist."""
import re
from pathlib import Path

from test_workspace_features import workspace_session, workspace_client  # noqa: F401

TOUR = Path(__file__).resolve().parent.parent / "app" / "static" / "tour.js"


def test_guide_button_and_script_on_every_page(workspace_client):
    page = workspace_client.get('/').text
    assert 'src="/static/tour.js' in page and 'data-tour-start' in page and 'Take the 2-minute tour' in page
    assert workspace_client.get('/static/tour.js').status_code == 200


def test_every_tour_step_page_loads(workspace_client):
    paths = set(re.findall(r"path: '([^']+)'", TOUR.read_text(encoding='utf-8')))
    assert {'/', '/worksheet', '/tables', '/inbox', '/customers', '/settings', '/settings/ai'} <= paths
    for path in paths:
        response = workspace_client.get(path)
        assert response.status_code == 200, path
        assert 'data-tour-start' in response.text, path
