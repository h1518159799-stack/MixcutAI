from publishers.postflow_confirmation import normalize_receipt, match_work, redirect_result
from publishers.postflow_adapter import _RUNNER


def test_verified_submission_is_published():
    result = normalize_receipt(dict(submitted=True, verified=True, remote_id='123',
        remote_url='https://channels.weixin.qq.com/platform/post/detail?id=123'))
    assert result['success'] and result['status'] == 'published'


def test_submitted_without_receipt_is_processing():
    result = normalize_receipt({'submitted': True})
    assert result['status'] == 'processing'
    assert not result['success'] and not result['verified'] and result['error_code'] == ''


def test_platform_error_overrides_submission_and_id():
    result = normalize_receipt(dict(submitted=True, verified=True, remote_id='123',
        remote_url='https://channels.weixin.qq.com/platform/post/detail?id=123',
        error_code='SUBMIT_FAILED', error_message='rejected'))
    assert result['status'] == 'failed' and not result['success']
    assert result['error_message'] == 'rejected'


def test_later_unique_work_confirms_processing():
    assert match_work([], 'title', 1000)['status'] == 'processing'
    work = dict(title='title', published_at=1010, remote_id='123',
        remote_url='https://channels.weixin.qq.com/platform/post/detail?id=123')
    assert match_work([work], 'title', 1000)['status'] == 'published'
    assert match_work([work, work], 'title', 1000)['status'] == 'processing'
    assert match_work([work], 'other title', 1000)['status'] == 'processing'
    assert match_work([work], 'title', 9000)['status'] == 'processing'


def test_missing_submission_is_not_assumed_submitted():
    assert normalize_receipt({})['status'] == 'waiting_confirmation'


def test_draft_identifier_is_not_a_receipt():
    result = normalize_receipt(dict(submitted=True, draftId='draft', clipKey='upload'))
    assert result['status'] == 'processing' and not result['remote_id']

def test_create_to_post_list_is_published_without_ids():
    result = redirect_result('https://channels.weixin.qq.com/platform/post/create',
        'https://channels.weixin.qq.com/platform/post/list', '视频号', '')
    assert result['success'] and result['verified'] and result['status'] == 'published'
    assert not result['remote_id'] and not result['remote_url']

def test_create_to_login_or_error_is_not_published():
    assert redirect_result('https://channels.weixin.qq.com/platform/post/create',
        'https://channels.weixin.qq.com/login', '', '')['status'] == 'failed'
    assert redirect_result('https://channels.weixin.qq.com/platform/post/create',
        'https://channels.weixin.qq.com/platform/error', '', '')['status'] == 'failed'

def test_create_without_redirect_remains_processing():
    assert redirect_result('https://channels.weixin.qq.com/platform/post/create',
        'https://channels.weixin.qq.com/platform/post/create', '', '')['status'] == 'processing'


def test_runner_compiles():
    compile(_RUNNER, '<postflow-runner>', 'exec')
