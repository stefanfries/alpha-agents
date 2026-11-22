import re
import shutil
import subprocess

import pytest

from app.config import settings
from app.routes.pipeline import STAGES, STAGE_LABELS, templates


def _render_screening():
    execution = {
        "execution_id": "exec1",
        "quant_system_id": "qs1",
        "config_overrides": {},
    }
    return templates.env.get_template("stages/screening.html").render(
        execution=execution,
        execution_id="exec1",
        qs_id="qs1",
        current_stage="screening",
        stages=STAGES,
        stage_labels=STAGE_LABELS,
        stage_status="awaiting_review",
        stage_progress=None,
        screening_cfg=settings.screening.model_dump(),
        currency_locale="de_DE",
        stage_result={
            "selected": [{"symbol": "A"}],
            "scores": {"A": 1.0, "B": 0.5},
            "history_labels": [],
            "rank_changes": {},
            "policy_results": {},
            "all_tickers": [{"symbol": "A"}, {"symbol": "B"}],
            "tq_short": {},
            "tsi": {},
            "weekly_confirmed": {},
            "trend_signals": {"A": "NEW", "B": "HOLD"},
        },
    )


def test_selected_entry_filter_and_row_markers():
    html = _render_screening()
    assert (
        html.index('id="filter-all"')
        < html.index('id="filter-selected"')
        < html.index('id="filter-new"')
    )
    assert 'for="filter-selected">Selected Entry Candidates</label>' in html
    assert not re.search(r'<input[^>]*id="filter-selected"[^>]*\bchecked\b', html)
    assert 'id="filter-selected" onchange="applySignalFilter(\'selected\')"' in html
    assert re.search(r'data-selected="true"\s+data-symbol="A"', html)
    assert re.search(r'data-selected="false"\s+data-symbol="B"', html)


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node.js is needed to exercise the inline filter",
)
def test_selected_entries_are_added_to_other_signal_filters():
    html = _render_screening()
    script = html.split("function applySignalFilter(", 1)[1].split(
        "\nfunction syncPolicyMinBounds(", 1
    )[0]
    script = "function applySignalFilter(" + script
    harness = """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const controls = Object.fromEntries(
  ['all', 'new', 'hold', 'break', 'none', 'selected'].map(id =>
    [`filter-${id}`, {checked: id === 'hold'}])
);
const rows = [
  {dataset: {selected: 'true'},  signal: 'NEW',   style: {}},
  {dataset: {selected: 'false'}, signal: 'HOLD',  style: {}},
  {dataset: {selected: 'true'},  signal: 'BREAK', style: {}},
  {dataset: {selected: 'false'}, signal: '',      style: {}}
];
rows.forEach(row => row.querySelector = () => ({dataset: {signal: row.signal}}));
const tbody = {querySelectorAll: () => rows};
const document = {getElementById: id => controls[id]};
const ctx = {document, screeningTableBody: () => tbody};
vm.runInNewContext(source + '\\nthis.apply = applySignalFilter;', ctx);
const visible = () => rows.map(row => row.style.display !== 'none');
ctx.apply();
assert.deepEqual(visible(), [false, true, false, false]);
controls['filter-selected'].checked = true;
ctx.apply('selected');
assert.deepEqual(visible(), [true, true, true, false]);
controls['filter-hold'].checked = false;
ctx.apply('hold');
assert.deepEqual(visible(), [true, false, true, false]);
controls['filter-selected'].checked = false;
ctx.apply('selected');
assert.deepEqual(visible(), [false, false, false, false]);
controls['filter-all'].checked = true;
ctx.apply('all');
assert.deepEqual(visible(), [true, true, true, true]);
controls['filter-selected'].checked = true;
ctx.apply('selected');
assert.equal(controls['filter-all'].checked, true);
controls['filter-selected'].checked = false;
ctx.apply('selected');
assert.equal(controls['filter-all'].checked, false);
assert.deepEqual(visible(), [true, true, true, true]);
"""
    subprocess.run(
        ["node", "-e", harness],
        input=script,
        text=True,
        check=True,
        capture_output=True,
    )
