"""Regression for the diagnostic's actual terminal-state polling failure."""
import importlib.util
from pathlib import Path
import pytest
import threading

spec=importlib.util.spec_from_file_location('capacity_harness',Path(__file__).with_name('validate_capacity.py'))
harness=importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


@pytest.mark.parametrize('state',['failed','needs_attention','cancelled'])
def test_non_successful_terminal_state_fails_immediately_instead_of_timing_out(state):
    with pytest.raises(RuntimeError,match='retime_terminal_failure'):
        harness.wait_for(lambda:harness.retime_ready([{'job':{'state':state},'asset_id':None}]),seconds=30)


def test_durable_result_requires_both_success_and_adopted_asset():
    assert harness.retime_ready([{'job':{'state':'running'},'asset_id':None}]) is None
    assert harness.retime_ready([{'job':{'state':'succeeded'},'asset_id':None}]) is None
    adopted=[{'job':{'state':'succeeded'},'asset_id':'synthetic-output'}]
    assert harness.retime_ready(adopted)==adopted


def test_monitor_abort_stops_stage_polling_before_more_work():
    event=threading.Event();event.set()
    with pytest.raises(RuntimeError,match='diagnostic_monitor_aborted'):
        harness.wait_for(lambda:pytest.fail('continued polling after resource abort'),abort=event)
