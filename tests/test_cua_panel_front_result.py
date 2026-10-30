"""Correct only independently verified file-panel bring_to_front refusals."""
import pytest
from box_agent.mcp_servers import cua_adapter as a
from tests.test_cua_adapter_port import FakeDriver, result, no_native_input_in_fake_driver_tests

@pytest.mark.parametrize('code', ['bring_to_front_exact_window_unverified','bring_to_front_window_not_ordinary'])
@pytest.mark.parametrize('proof_kind', ['valid','missing','refused','wrong_pid','wrong_window','wrong_code','exception'])
def test_panel_bring_result_requires_exact_readonly_proof(monkeypatch, code, proof_kind):
    d=FakeDriver(); api=a.Adapter(d)
    original={'isError':True, **result(code=code,pid=42,window_id=7,activated=False)}
    d.responses['bring_to_front']=original
    proof={'effect':'confirmed','code':'file_panel_front_verified','pid':42,'window_id':7}
    if proof_kind=='missing':proof=None
    elif proof_kind=='refused':proof['effect']='not_sent'
    elif proof_kind=='wrong_pid':proof['pid']=99
    elif proof_kind=='wrong_window':proof['window_id']=8
    elif proof_kind=='wrong_code':proof['code']='other'
    def verify(*args,**kwargs):
        assert args==(42,7)
        if proof_kind=='exception':raise RuntimeError('verification unavailable')
        return proof
    monkeypatch.setattr(a,'verify_file_panel_front',verify)
    if proof_kind=='valid':
        got=api.call('bring_to_front',{'pid':42,'window_id':7})
        assert not a.failed(got)
        assert a.data(got)['exact_window_effect']['verified'] is True
        assert a.data(got)['driver_result']==original['structuredContent']
    else:
        with pytest.raises(a.ActionError) as e:api.call('bring_to_front',{'pid':42,'window_id':7})
        assert e.value.result == original
    assert [n for n,_ in d.calls]==['bring_to_front']

@pytest.mark.parametrize('report', [
    {'code':'other','pid':42,'window_id':7},
    {'code':'bring_to_front_exact_window_unverified','pid':43,'window_id':7},
    {'code':'bring_to_front_exact_window_unverified','pid':42,'window_id':8},
])
def test_unrelated_bring_error_does_not_run_native_verifier(monkeypatch, report):
    d=FakeDriver();api=a.Adapter(d);d.responses['bring_to_front']={'isError':True,**result(**report)}
    monkeypatch.setattr(a,'verify_file_panel_front',lambda *x,**k:pytest.fail('unexpected verification'))
    with pytest.raises(a.ActionError):api.call('bring_to_front',{'pid':42,'window_id':7})
