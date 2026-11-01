"""Deterministic selection of a frozen library name, never a model decision."""
from __future__ import annotations

import re
import unicodedata
import hashlib
from copy import deepcopy


# Enumerate one final particle; never strip words from the actual reply.
# Name/ordinal collisions still collect every possible frozen library ID.
_SELECTION_ENDINGS = ('', '。', '.', '！', '!', '吧', '吧。', '吧.', '吧！', '吧!')


def normalized_choice(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError('invalid_library_selection_text')
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def _chinese_menu_number(number):
    """Standard menu numbers1..128, with no colloquial or approximate forms."""
    digits = '零一二三四五六七八九'
    if number < 10:
        return digits[number]
    if number < 20:
        return '十' + (digits[number % 10] if number % 10 else '')
    if number < 100:
        return digits[number // 10] + '十' + (digits[number % 10] if number % 10 else '')
    rest = number - 100
    return '一百' + ('' if not rest else '零' + digits[rest] if rest < 10
                    else '一十' + (digits[rest % 10] if rest % 10 else '') if rest < 20
                    else _chinese_menu_number(rest))


def selection_forms(options):
    """Keep original directory ordinals; ambiguous name/number matches stay ambiguous."""
    result = {}
    ordinals = set()
    for index, item in enumerate(options, 1):
        key, name = item.get('knowledge_base_id'), item.get('name')
        if type(key) is not int or key <= 0 or not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            raise ValueError('invalid_frozen_library_option')
        ordinal = item.get('ordinal', index)
        if type(ordinal) is not int or not 1 <= ordinal <= 128 or ordinal in ordinals:
            raise ValueError('invalid_frozen_library_ordinal')
        ordinals.add(ordinal)
        ordinal_forms = [str(ordinal)]
        for number in (str(ordinal), _chinese_menu_number(ordinal)):
            ordinal_forms.extend((f'第{number}个', f'第{number}个知识库',
                f'选第{number}个', f'选择第{number}个', f'保存到第{number}个', f'放到第{number}个'))
        for phrase in ordinal_forms:
            for ending in _SELECTION_ENDINGS:
                result.setdefault(normalized_choice(phrase + ending), set()).add(key)
        forms = {name, name + '知识库'}
        forms.update(left + name + right for left, right in [('“', '”'), ('「', '」'), ('"', '"'), ("'", "'")])
        for form in forms:
            for prefix in ('', '选', '选择', '我选', '存到', '保存到', '存进', '放进', '放到', '上传到',
                           '收藏到', '加到', '加入到'):
                phrase = normalized_choice(prefix + form)
                # A negative/conditional utterance is never a save consent just
                # because its words resemble a strangely named library.
                if re.match(r'^(?:不要|别|不存|不用|取消|如果|怎么|如何|删除)', phrase):
                    continue
                for ending in _SELECTION_ENDINGS:
                    result.setdefault(phrase + ending, set()).add(key)
    return result


def selection_value_from_public_text(value, receiver_reply_label=''):
    """Remove only the observed canonical B reply label, not arbitrary words."""
    text = normalized_choice(value)
    if not receiver_reply_label:
        return text
    label = normalized_choice(receiver_reply_label)
    if len(label) > 80 or re.search(r'https?://', label, re.I):
        raise ValueError('invalid_choice_receiver_reply_label')
    for prefix in ('回复' + label + ':', '回复' + label, label + ':', label):
        if text.startswith(prefix):
            remaining = text[len(prefix):].lstrip(':')
            if remaining:
                return remaining
    raise ValueError('choice_reply_label_not_in_original_body')


_SOLE_LIBRARY_MARKERS = ('就一个', '只有一个', '唯一', '唯一的', '默认库', '默认知识库', '那个库', '这个库')
_NEGATIVE_SELECTION = re.compile(r'不要|别|不存|不用|取消|不能|不想|先别')


def _single_option_intent(text):
    """A natural reply that names no library but points at the only option."""
    if not isinstance(text, str) or _NEGATIVE_SELECTION.search(text):
        return False
    return any(marker in text for marker in _SOLE_LIBRARY_MARKERS)


def parse_library_selection(value, options):
    key = normalized_choice(value)
    matches = selection_forms(options).get(key, set())
    if len(matches) != 1:
        if len(options) == 1 and _single_option_intent(key):
            return options[0]
        return None
    selected = next(iter(matches))
    return next(item for item in options if item['knowledge_base_id'] == selected)


def semantic_choice_catalog(options):
    """Only the frozen, import-authorized menu; never expand to a new grant."""
    from app.mention_agent.contracts import canonical_json
    if type(options) is not list or not 1<=len(options)<=128:
        raise ValueError('invalid_semantic_choice_catalog')
    result=[];grants=set();ordinals=set()
    for index,item in enumerate(options,1):
        if not isinstance(item,dict):raise ValueError('invalid_semantic_choice_catalog')
        auth=item.get('authorization');identity=item.get('knowledge_base_id');ordinal=item.get('ordinal',index)
        if (not isinstance(auth,dict) or type(identity) is not int or identity<=0
                or auth.get('knowledge_base_id')!=identity or auth.get('action')!='import'
                or type(auth.get('grant_id')) is not str or not auth['grant_id']
                or item.get('can_import',True) is not True
                or type(item.get('name')) is not str or not 1<=len(item['name'].strip())<=128
                or type(ordinal) is not int or not 1<=ordinal<=128
                or auth['grant_id'] in grants or ordinal in ordinals):
            raise ValueError('invalid_semantic_choice_catalog')
        grants.add(auth['grant_id']);ordinals.add(ordinal)
        # The old persisted menu contains only _eligible_options(import).
        # No query permission or unseen alias is invented for those entries.
        result.append({'grant_id':auth['grant_id'],'name':item['name'],'aliases':[],
                       'can_query':False,'can_import':True,'ordinal':ordinal})
    return {'revision':hashlib.sha256(canonical_json(options).encode('utf-8')).hexdigest(),'options':result}


def semantic_choice_owner(session,envelope):
    """Same source identity used by planning and the reservation transaction."""
    root=session['root_envelope'];binding=session['root_binding'];auth=session['authorization_snapshot']
    proof=envelope.get('evidence',{})
    if (any(envelope.get(key)!=root.get(key) for key in ('sender_profile_url','receiver_profile_url','video_id'))
            or proof.get('root_reference_id')!=root['reference_id'] or proof.get('choice_id')!=session['choice_id']
            or proof.get('selection_reference_id')!=envelope.get('reference_id')):
        raise ValueError('semantic_choice_owner_changed')
    return {'user_id':auth['user_id'],'device_id':auth['device_id'],'instance_id':binding['instance_id'],
        **{key:envelope[key] for key in ('sender_profile_url','receiver_profile_url','video_id','reference_id')},
        'root_reference_id':root['reference_id']}


def choice_prompt(options, *, catalog_count, catalog_scope='authorized_libraries', max_chars=160):
    """Ask for a library the way a person would in a comment thread."""
    if type(catalog_count) is not int or catalog_count < 0 or catalog_scope not in {'all_local', 'authorized_libraries'}:
        raise ValueError('invalid_library_catalog_scope')
    if not isinstance(options, list) or len(options) > 128:
        raise ValueError('invalid_library_catalog')
    available = [item for item in options if item.get('can_import', item.get('authorization') is not None)]
    if not available:
        return '现在还没有能保存的库，先去本机设置里授权一下吧，这个视频先不存。'
    names = []
    for index, item in enumerate(available, 1):
        name = item.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            raise ValueError('invalid_library_catalog_name')
        display = re.sub(r'[\r\n\t]+', ' ', name).strip()
        if '@' in display or re.search(r'https?://', display, re.I):
            display = '特殊名称库（完整名称见本机）'
        ordinal = item.get('ordinal', index)
        if type(ordinal) is not int or not 1 <= ordinal <= 128:
            raise ValueError('invalid_library_catalog_ordinal')
        names.append((ordinal, display))
    shown = '、'.join(f'{ordinal}. {display}' for ordinal, display in names[:3])
    if len(names) > 3:
        shown += ' 等'
    count = len(available)
    variants = (
        f'你有{count}个知识库能存：{shown}。想放这个的话，回我名字或序号就行',
        f'要放哪个库？你有{count}个知识库能选：{shown}。说下库名或序号哈',
        f'选个库吧，你有{count}个知识库能存：{shown}。直接回名字或序号就好',
    )
    value = variants[(sum(ordinal for ordinal, _ in names) + len(names)) % len(variants)]
    if len(value) > max_chars:
        first = names[0][1]
        value = f'要存到哪个库？现在能选「{first}」' + ('等' if len(names) > 1 else '') + '，回名字或序号就行'
    if len(value) > max_chars:
        raise ValueError('library_choice_prompt_budget_exceeded')
    return value
