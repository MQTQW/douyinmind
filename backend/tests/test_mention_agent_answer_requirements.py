"""Count, current-tool provenance, public visibility and bounded correction.

Pure synthetic model/tool fixtures: no provider, Chroma, DB, import or publisher.
"""
from copy import deepcopy
import json

import pytest

from app.mention_agent.answer_requirements import (
    AnswerRequirementError, QuoteRequirement, parse_answer_requirements, validate_quotes,
)
from app.mention_agent.engine import AgentEngine, TOOLS
from tests.test_mention_agent_engine_review import binding, envelope, call


FIRST = "先检查土壤是否已经湿润。"
SECOND = "如果盆土已经干燥，再用清水缓慢浇灌。"
THIRD = "浇水后应让多余的水分自然流出。"
TEXT = FIRST+"\n"+SECOND+"\n"+THIRD
COMMENT = "请说明如何照顾盆栽，并给出两条原文依据。"


class Knowledge:
    def __init__(self, text=TEXT):
        self.text, self.queries = text, []
    def describe(self):
        return {"id":7,"name":"测试库","total_count":1,"ready_count":1}
    def query(self, question):
        self.queries.append(question)
        return {"status":"ok","knowledge_base_id":7,"knowledge_base_name":"测试库","retrieval_mode":"semantic",
                "evidence":[{"source_id":"资料1","platform_item_id":"7999999000000000022","chunk_id":"synthetic:1",
                             "title":"合成盆栽说明","url":"https://www.douyin.com/video/7999999000000000022","text":self.text}]}


class Model:
    def __init__(self, turns):
        self.turns=list(turns);self.requests=[];self.tools=[]
    def complete(self,messages,tools):
        self.requests.append(deepcopy(messages));self.tools.append(deepcopy(tools))
        if not self.turns:
            raise AssertionError("test budget must not need extra turns")
        return self.turns.pop(0)


def query(identity="query-1"):
    return call("query_knowledge_base",{"knowledge_base_id":7,"question":"怎样照顾盆栽？"},identity)


def quotes(*excerpts, source="资料1"):
    return [{"source_id":source,"excerpt":text} for text in excerpts]


def answer(excerpts=(FIRST,SECOND), *, text=None, structured=True, identity="reply-1"):
    data={"text":text if text is not None else "；".join(f"{i+1}.“{quote}”" for i,quote in enumerate(excerpts)),
          "source_ids":["资料1"]}
    if structured:
        data["evidence_quotes"]=quotes(*excerpts)
    return call("propose_reply",data,identity)


def clarify(identity="clarify-1"):
    return call("request_clarification",{"reason_code":"quote_requirement_unmet"},identity)


def run(turns, *, comment=COMMENT, text=TEXT, **engine_options):
    records=[];model=Model(turns);knowledge=Knowledge(text)
    decision=AgentEngine(model,knowledge,**engine_options).run(binding(),envelope(comment),record=lambda *items:records.append(items))
    return decision,model,knowledge,records


@pytest.mark.parametrize("comment,minimum,maximum",[
    ("给出两条原文依据",2,2),("@助手 请引用2句原话",2,2),("请附上三段原文",3,3),
    ("请提供１条原文证据",1,1),("请给出两条简短的原文依据",2,2),
    ("至少给两条不同的原文",2,3),("最多给两条原文依据",1,2),
    ("不要超过两条原文",1,2),("原文依据最多两条",1,2),("原文不要超过三条",1,3),
    ("请引用两句",2,2),("给我原话2段",2,2),("两条原文依据",2,2),
    ("不要两条原文，而是三条原文",3,3),("不用两条原文，只给一句原话",1,1),
    ("至少两条原文，最多三条原文",2,3),
    ("用两条原文解释",2,2),("给出两个原文片段",2,2),("附上两条摘录",2,2),
    ("原话给出两段",2,2),
    ("请不要少于两条原文依据",2,3),
    ("不要总结而是给出两条原文依据",2,2),
])
def test_explicit_quote_counts_and_bound_modifiers(comment,minimum,maximum):
    requirement=parse_answer_requirements(comment)
    assert requirement==QuoteRequirement(minimum,maximum)


@pytest.mark.parametrize("comment",[
    "请用两句话总结这条视频","请列出三个建议","分别说说两个区别",
    "不要给出两条原文依据","不需要三段原文","别引用两句原文","不用引用2句",
    "视频提到两条原文吗","我是否需要两条原文依据","请解释三个区域而不是两条原文",
    "不需要原文，解释两点就好","请根据资料回答这个问题","请引用原文",
])
def test_nonrequirements_negation_and_summary_length_do_not_force_quotes(comment):
    assert parse_answer_requirements(comment) is None


def test_conflicting_or_over_limit_count_is_a_clear_fixed_clarification_without_model():
    for comment in ["给四条原文依据","请给10句原文","给出两条原文，至少三条原文"]:
        decision,model,knowledge,_=run([],comment=comment)
        assert decision.proposal["kind"]=="clarification"
        assert model.requests==[] and knowledge.queries==[]
        assert decision.model_rounds==decision.tool_calls==0


def test_two_distinct_quotes_from_one_chunk_satisfy_two_basis_request():
    decision,model,knowledge,records=run([query(),answer()])
    assert decision.proposal["kind"]=="answer"
    assert len(decision.proposal["sources"])==1 and decision.proposal["source_ids"]==["资料1"]
    assert FIRST in decision.proposal["text"] and SECOND in decision.proposal["text"]
    assert "[来源" not in decision.proposal["text"]
    audit=next(meta for kind,meta in records if kind=="tool_result" and meta["status"]=="reply_proposed")
    assert len(audit["answer_requirement"]["quotes"])==2
    assert [item["excerpt"] for item in audit["answer_requirement"]["quotes"]]==[FIRST,SECOND]
    assert len(model.requests)==2 and len(knowledge.queries)==1


def test_a_single_source_summary_without_declared_quotes_is_rejected_then_corrected_in_budget():
    decision,model,_,records=run([query(),answer(text="原文建议先查看土壤，再按实际情况浇水。",structured=False),answer(identity="reply-2")])
    assert decision.proposal["kind"]=="answer" and len(model.requests)==3
    errors=[meta for kind,meta in records if kind=="tool_result" and meta["status"]=="error"]
    assert len(errors)==1
    message=model.requests[2][-1]
    assert json.loads(message["content"])["code"]=="evidence_quotes_required"
    assert "同一资料块可以给两条" in json.loads(message["content"])["hint"]
    assert sum(kind=="decision" for kind,_ in records)==1


def test_hidden_valid_quote_payload_cannot_pass_without_actual_public_quotes():
    decision,model,_,_=run([query(),answer(text="先查看土壤，再按实际情况浇水。"),clarify()])
    assert decision.proposal["kind"]=="clarification"
    assert "未生成满足" in decision.proposal["text"]
    assert "没有找到" not in decision.proposal["text"], "format failure is not proof the library lacks facts"
    assert json.loads(model.requests[2][-1]["content"])["code"]=="evidence_quote_not_visible"


def test_literal_text_in_a_summary_is_not_a_distinct_visible_quotation():
    decision,model,_,_=run([query(),answer(text=FIRST+SECOND),clarify()])
    assert decision.proposal["kind"]=="clarification"
    assert json.loads(model.requests[2][-1]["content"])["code"]=="evidence_quote_not_visible"


def test_third_public_long_quote_cannot_hide_outside_structured_payload():
    body='1.“'+FIRST+'”；2.“'+SECOND+'”；另有“所有盆栽每天都必须浇十次水。”'
    decision,model,_,_=run([query(),answer(text=body),clarify()],comment='请最多给两条原文依据')
    assert decision.proposal['kind']=='clarification'
    assert len(model.requests)==3


def test_hidden_excerpt_length_limit_cannot_be_bypassed_by_whitespace():
    ordinary='先执行 git add 再确认状态。'
    huge=ordinary.replace('git add','git'+' '*1000+'add')
    with pytest.raises(AnswerRequirementError,match='invalid_evidence_quote'):
        validate_quotes(QuoteRequirement(1,1),quotes(huge),'“'+ordinary+'”',['资料1'],{'资料1':[ordinary]})


def test_unlisted_third_quote_is_rejected_even_when_at_least_range_allows_three():
    body='“'+FIRST+'”“'+SECOND+'”“'+THIRD+'”'
    with pytest.raises(AnswerRequirementError,match='unaccounted_public_quote'):
        validate_quotes(QuoteRequirement(2,3),quotes(FIRST,SECOND),body,['资料1'],{'资料1':[TEXT]})


def test_short_terminology_and_trusted_library_name_are_not_extra_basis():
    body='在“演示专用的盆栽知识库”中，“土壤”是术语。1.“'+FIRST+'”；2.“'+SECOND+'”'
    assert validate_quotes(QuoteRequirement(2,2),quotes(FIRST,SECOND),body,['资料1'],{'资料1':[TEXT]},
                           non_evidence_labels=['演示专用的盆栽知识库'])
    with pytest.raises(AnswerRequirementError,match='public_evidence_quote_count'):
        validate_quotes(QuoteRequirement(2,2),quotes(FIRST,SECOND),body,['资料1'],{'资料1':[TEXT]})


def test_explicit_third_short_basis_is_counted_not_hidden_as_terminology():
    body='1.“'+FIRST+'”；2.“'+SECOND+'”；原文3：“天天浇水。”'
    with pytest.raises(AnswerRequirementError,match='public_evidence_quote_count'):
        validate_quotes(QuoteRequirement(1,2),quotes(FIRST,SECOND),body,['资料1'],{'资料1':[TEXT]})


@pytest.mark.parametrize('introducer',['第三条：','另一条原文是','另一条原文写道：'])
def test_short_extra_quote_with_explicit_evidence_introduction_is_counted(introducer):
    body='1.“'+FIRST+'”；2.“'+SECOND+'”；'+introducer+'“天天浇水”'
    with pytest.raises(AnswerRequirementError,match='public_evidence_quote_count'):
        validate_quotes(QuoteRequirement(1,2),quotes(FIRST,SECOND),body,['资料1'],{'资料1':[TEXT]})


@pytest.mark.parametrize("bad,code",[
    ([FIRST,FIRST],"duplicate_evidence_quote"),
    ([FIRST,FIRST[2:]],"duplicate_evidence_quote"),
    ([FIRST,"所有盆栽每天都必须浇很多水。"],"evidence_quote_not_provided"),
])
def test_duplicate_contained_or_invented_quotes_cannot_pass(bad,code):
    decision,model,_,_=run([query(),answer(excerpts=bad),clarify()])
    assert decision.proposal["kind"]=="clarification"
    assert json.loads(model.requests[2][-1]["content"])["code"]==code


def test_tool_truncation_not_underlying_retriever_full_document_is_the_authority():
    hidden=("已显示的无关前缀。"*20)+TEXT
    decision,model,_,_=run([query(),answer(),clarify()],text=hidden,evidence_chars=100)
    actual=json.loads(model.requests[1][-1]["content"])["evidence"][0]["text"]
    assert len(actual)==100 and FIRST not in actual
    assert decision.proposal["kind"]=="clarification"
    assert json.loads(model.requests[2][-1]["content"])["code"]=="evidence_quote_not_provided"


def test_old_conversation_quotes_are_not_current_tool_evidence():
    history={"prior_questions":["之前说了什么？"],"previous_answer":"“"+FIRST+"”“"+SECOND+"”"}
    decision,model,_,_=run([query(),answer(),clarify()],text="当前资料只说明清理花盆边缘，不包含之前的两段话。",conversation_context=history)
    assert decision.proposal["kind"]=="clarification"
    assert json.loads(model.requests[2][-1]["content"])["code"]=="evidence_quote_not_provided"


def test_whitespace_only_normalization_does_not_allow_changed_words_or_punctuation():
    requirement=QuoteRequirement(1,1)
    excerpt="先检查 土壤是否已经湿润。"
    assert validate_quotes(requirement,quotes(excerpt),"“"+excerpt+"”",["资料1"],
                           {"资料1":["先检查\n土壤是否已经湿润。"]})
    changed="先检查土壤是否已经湿润!"
    with pytest.raises(AnswerRequirementError,match="evidence_quote_not_provided"):
        validate_quotes(requirement,quotes(changed),"“"+changed+"”",["资料1"],{"资料1":[FIRST]})


def test_at_least_and_at_most_use_ranges_not_different_source_count():
    decision,*_=run([query(),answer(excerpts=(FIRST,SECOND,THIRD))],comment="请至少给两条原文依据")
    assert decision.proposal["kind"]=="answer"
    decision,*_=run([query(),answer(excerpts=(FIRST,))],comment="最多给两条原文依据")
    assert decision.proposal["kind"]=="answer"


def test_no_explicit_count_preserves_old_short_answer_schema_and_behavior():
    before=deepcopy(TOOLS)
    decision,model,_,_=run([query(),answer(text="资料建议查看土壤后按需浇水。",structured=False)],comment="请概括照顾盆栽的方法。")
    assert decision.proposal["kind"]=="answer"
    schema=next(tool for tool in model.tools[0] if tool["function"]["name"]=="propose_reply")
    assert "evidence_quotes" not in schema["function"]["parameters"]["properties"]
    assert TOOLS==before
    run([query(),answer()])
    assert TOOLS==before, "requirement tools must not mutate the shared legacy schema"


def test_invalid_quote_format_cannot_extend_max_rounds_or_return_answer_intent():
    decision,model,_,_=run([query(),answer(structured=False)],max_rounds=2)
    assert decision.proposal is None and len(model.requests)==2
    assert decision.review_code=="model_round_budget_exceeded"


def test_insufficiency_clarification_cannot_skip_current_library_query():
    decision,model,knowledge,_=run([clarify(),query(),clarify(identity="clarify-2")])
    assert decision.proposal["kind"]=="clarification" and len(knowledge.queries)==1
    first_error=json.loads(model.requests[1][-1]["content"])
    assert first_error["code"]=="query_required_before_quote_clarification"


class BudgetError(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__("SYNTHETIC_PRIVATE_BUDGET_MESSAGE")


@pytest.mark.parametrize("code,expected",[
    ("model_budget_exceeded","model_budget_exceeded"),
    ("model_budget_unknown","model_budget_unknown"),("untrusted-private-code","model_preflight_failed"),
    (["model_budget_exceeded"],"model_preflight_failed"),
])
def test_budget_preflight_denial_does_not_record_or_send_provider_request(code,expected):
    class Denied(Model):
        def preflight(self,messages,tools):
            self.preflight_tools=deepcopy(tools)
            raise BudgetError(code)
    records=[];model=Denied([])
    decision=AgentEngine(model,Knowledge()).run(binding(),envelope(COMMENT),record=lambda *args:records.append(args))
    assert decision.review_code==expected and decision.model_rounds==0
    assert not model.requests and not any(kind=="model_request" for kind,_ in records)
    assert "SYNTHETIC_PRIVATE" not in repr(records)
    schema=next(item["function"] for item in model.preflight_tools if item["function"]["name"]=="propose_reply")
    assert "evidence_quotes" in schema["parameters"]["required"]


def test_preflight_and_complete_receive_same_selected_tools_in_correct_order():
    events=[]
    class Budgeted(Model):
        expected=None
        def preflight(self,messages,tools):
            self.expected=tools;events.append("preflight")
        def complete(self,messages,tools):
            assert tools is self.expected
            events.append("complete")
            return super().complete(messages,tools)
    model=Budgeted([query(),answer()])
    def record(kind,metadata):
        if kind=="model_request":events.append("model_request")
    assert AgentEngine(model,Knowledge()).run(binding(),envelope(COMMENT),record=record).proposal["kind"]=="answer"
    assert events==["preflight","model_request","complete"]*2


def test_budget_failure_during_complete_retains_attempt_but_no_completed_response():
    class FailedComplete(Model):
        def preflight(self,*args):pass
        def complete(self,*args):raise BudgetError("model_budget_unknown")
    records=[]
    decision=AgentEngine(FailedComplete([]),Knowledge()).run(binding(),envelope(COMMENT),record=lambda *args:records.append(args))
    assert decision.review_code=="model_budget_unknown"
    assert sum(kind=="model_request" for kind,_ in records)==1
    assert not any(kind=="model_response" for kind,_ in records)
    assert "SYNTHETIC_PRIVATE" not in repr(records)


def test_revocation_while_preflight_waits_prevents_new_provider_request():
    active=True;records=[]
    class Revoking(Model):
        def preflight(self,*args):
            nonlocal active
            active=False
    model=Revoking([])
    decision=AgentEngine(model,Knowledge()).run(binding(),envelope(COMMENT),
        current_binding=lambda:binding() if active else None,record=lambda *args:records.append(args))
    assert decision.review_code=='binding_changed' and not model.requests
    assert not any(kind=='model_request' for kind,_ in records)
