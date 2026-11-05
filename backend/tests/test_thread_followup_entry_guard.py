"""Operator review flags never double as a registered runtime launch."""
import pytest
from app.assistant import __main__ as entry


@pytest.mark.parametrize('option', ['--review-thread-followup', '--inspect-thread-followup', '--inspect-original-source'])
def test_review_and_runtime_cannot_be_combined_before_any_database_open(monkeypatch, option):
    def forbidden(*args, **kwargs):
        pytest.fail('conflicting CLI flags must be rejected before opening a database')
    monkeypatch.setattr(entry, 'AssistantQueue', forbidden)
    with pytest.raises(SystemExit) as result:
        entry.main(['--run-registered', option, 'native:'+'a'*64])
    assert result.value.code == 2


@pytest.mark.parametrize('configure',['--enable-intent-clarification','--disable-intent-clarification'])
def test_original_source_inspection_cannot_mix_with_configuration(monkeypatch,configure):
    monkeypatch.setattr(entry,'AssistantQueue',lambda *a,**k:pytest.fail('conflicting flags opened a database'))
    with pytest.raises(SystemExit) as result:
        entry.main([configure,'--inspect-original-source','native:'+'a'*64])
    assert result.value.code==2


def test_original_source_inspection_cli_registers_only_existing_owner_mailbox(monkeypatch,capsys):
    from types import SimpleNamespace
    from app.assistant import native_recovery_mailbox
    calls=[]
    control=SimpleNamespace(snapshot=lambda:{'run_id':'b'*32},
        start=lambda:pytest.fail('inspection CLI started a process'))
    monkeypatch.setattr(entry,'AssistantQueue',lambda *a,**k:SimpleNamespace())
    monkeypatch.setattr(entry,'AssistantControl',lambda *a,**k:control)
    monkeypatch.setattr(entry,'start_guard',lambda:None)
    monkeypatch.setattr(entry,'run_registered',lambda *a:pytest.fail('inspection CLI ran a browser owner'))
    def mailbox(state_dir,run_id,actual_control):
        assert state_dir==entry.STATE and run_id=='b'*32 and actual_control is control
        def submit(source,operation):
            calls.append((source,operation))
            return {'status':'queued','dispatchable':False,'no_model':True,'no_import':True,'no_send':True}
        return SimpleNamespace(submit=submit)
    monkeypatch.setattr(native_recovery_mailbox,'NativeRecoveryMailbox',mailbox)
    assert entry.main(['--inspect-original-source','native:'+'a'*64])==0
    assert calls==[('native:'+'a'*64,'inspect_original_source')]
    assert 'queued' in capsys.readouterr().out
