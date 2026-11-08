import os
import pytest
from media_tag.deployment.capacity import request_summary,browser_summary,directory_usage,capacity_projection


def test_failed_and_timed_out_requests_remain_in_latency_and_error_denominators():
    rows=[{'elapsed_ms':10,'ok':True} for _ in range(18)]+[
        {'elapsed_ms':2000,'ok':False},{'elapsed_ms':10000,'ok':False,'timeout':True}]
    result=request_summary(rows)
    assert result=={'count':20,'errors':2,'timeouts':1,'error_rate':.1,'p50_ms':10,'p95_ms':2000,'max_ms':10000}
    assert request_summary([])['error_rate'] is None


@pytest.mark.parametrize('duration',[True,-1,float('inf'),float('nan')])
def test_invalid_latency_is_not_silently_used(duration):
    with pytest.raises(ValueError):request_summary([{'elapsed_ms':duration,'ok':True}])


def test_capacity_does_not_double_count_hardlinks_or_follow_linked_directories(tmp_path):
    root=tmp_path/'data';root.mkdir();(root/'a').write_bytes(b'123')
    os.link(root/'a',root/'b')
    assert directory_usage(root)['logical_bytes']==3
    # Use a real directory link where supported; Windows test accounts may lack that right.
    elsewhere=tmp_path/'outside';elsewhere.mkdir();(elsewhere/'private').write_bytes(b'outside')
    try:(root/'linked').symlink_to(elsewhere,target_is_directory=True)
    except OSError:pass
    else:
        result=directory_usage(root);assert result['logical_bytes']==3 and result['skipped_links']==1
    assert directory_usage(tmp_path/'missing')['exists'] is False


def test_projection_names_the_actual_staging_and_backup_assumptions():
    result=capacity_projection(100,150,50)
    assert result['live_to_input_ratio']==2
    assert result['one_input_copy_and_one_live_backup_ratio']==5
    with pytest.raises(ValueError):capacity_projection(0,1,1)


def test_cold_page_and_first_query_do_not_get_mixed_with_warm_measurements():
    result=browser_summary({'rows':[{'elapsed_ms':30,'ok':True}],
        'cold':[{'elapsed_ms':1200,'ok':True},{'kind':'first_search','elapsed_ms':400,'ok':True}]})
    assert result['warm']['p95_ms']==30
    assert result['cold_page']['p95_ms']==1200
    assert result['cold_query']['p95_ms']==400
