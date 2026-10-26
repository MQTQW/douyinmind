"""Optional schema probe for one public test; never an intake permission."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re


def load_visibility_probe(state_dir):
    try:
        from .native_configuration import _owned
        from .native_events import _visibility_probe
        path=_owned(Path(state_dir)/'native-visibility-probe.json')
        if not path.is_file() or path.stat().st_size>2048:return None
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise ValueError()
                result[key]=value
            return result
        value=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        return _visibility_probe(value)
    except Exception:
        return None  # Diagnostic configuration cannot change intake behavior.


_BOOLS={'video_id_valid','video_matches_probe','comment_id_valid','content_is_string','current_text_matches'}
_NULL_BOOLS={'current_comment_id_matches','previous_comment_id_matches','previous_text_matches'}
_CATEGORIES={'missing','null','bool','int0','int1','other_int','string','other'}
_KEYS={'channel','notice_type','at_sub_type','notice_id_sha256','visible_category','hidden_category'}|_BOOLS|_NULL_BOOLS


def record_visibility_probe(control,run_id,lane):
    """Project only fixed diagnostic values after a successful native poll."""
    try:
        receiver=getattr(lane,'native_receiver',None)
        if receiver is None or getattr(receiver,'visibility_diagnostic_probe',None) is None:return False
        rows=getattr(receiver,'last_visibility_diagnostics',None)
        if type(rows) is not list or len(rows)>16:return False
        safe=[]
        for row in rows:
            if (type(row) is not dict or set(row)!=_KEYS
                    or any(type(row[k]) is not int for k in ('channel','notice_type','at_sub_type'))
                    or (row['channel'],row['notice_type'],row['at_sub_type'])!=(9,45,2)
                    or type(row['notice_id_sha256']) is not str
                    or re.fullmatch('[a-f0-9]{64}',row['notice_id_sha256']) is None
                    or any(type(row[k]) is not str or row[k] not in _CATEGORIES for k in ('visible_category','hidden_category'))
                    or any(type(row[k]) is not bool for k in _BOOLS)
                    or any(row[k] is not None and type(row[k]) is not bool for k in _NULL_BOOLS)):
                return False
            safe.append(dict(row))
        value={'scope':'schema_probe_only_not_visibility_or_execution_evidence',
               'observed_at_utc':datetime.now(timezone.utc).isoformat(),'items':safe}
        def update(current):
            if current.get('run_id')!=run_id:raise ValueError('runtime_owner_changed')
            current['native_visibility_diagnostics']=value
            return current
        control.queue.update_control(update)
        return True
    except Exception:
        return False  # Optional diagnostic writes cannot stall the receiver.
