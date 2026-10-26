"""Fixed local opt-in; no HTTP caller can select a model or ledger path."""
import json
from pathlib import Path
from .native_configuration import _owned,NativeConfigurationError


def semantic_intent_enabled(state_dir):
    path=_owned(Path(state_dir)/'semantic-intent.json')
    ledger=Path(state_dir)/'semantic-plans.sqlite3'
    if not path.exists():
        if ledger.exists() or ledger.with_suffix(ledger.suffix+'.owner.json').exists():
            raise NativeConfigurationError('semantic_configuration_required')
        return False
    try:
        if path.stat().st_size>256:raise ValueError()
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise ValueError()
                result[key]=value
            return result
        value=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value)!={'version','enabled'}
                or type(value['version']) is not int or value['version']!=1
                or type(value['enabled']) is not bool):raise ValueError()
        return value['enabled']
    except Exception:
        raise NativeConfigurationError('semantic_configuration_invalid') from None


def build_semantic_interpreter(state_dir,registry,budget,*,should_stop,protected_paths=()):
    if not semantic_intent_enabled(state_dir):return None
    from .semantic_plan_store import SemanticPlanStore
    from .semantic_intent_model import default_semantic_model
    from .semantic_routing import SemanticIntentInterpreter
    store=SemanticPlanStore.prepare(Path(state_dir)/'semantic-plans.sqlite3',protected_paths=protected_paths)
    return SemanticIntentInterpreter(registry,store,default_semantic_model(),budget,should_stop=should_stop)
