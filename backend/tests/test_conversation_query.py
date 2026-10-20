"""Conservative follow-up context from synthetic topics, no model or database."""
import pytest

from app.services.conversation_query import build_retrieval_query, needs_followup_context


@pytest.mark.parametrize("query", [
    "它为什么会失效？", "这种情况怎么处理？", "那个方法有什么限制？",
    "刚才提到的办法安全吗？", "前者和后者有什么区别？", "第二条是什么意思？",
    "第2步怎么做？", "你说的那个方法呢？", "上述结论有什么条件？",
    "还有呢？", "继续解释", "请再具体说说。", "然后呢", "还有其他原因吗？",
    "What does it mean?", "How does that work?", "What about the latter?",
])
def test_explicit_references_and_short_continuations_need_context(query):
    assert needs_followup_context(query)


@pytest.mark.parametrize("query", [
    "那离心泵呢？", "那么光纤怎样传输信号？", "太阳能电池有什么优点？",
    "我想了解海洋生态", "继续解释离心泵的工作原理", "还有一种叫碳纤维的材料有什么特点？",
    "换个话题，这个望远镜怎样成像？", "新问题：它是什么？",
    "不要参考历史，这个问题单独回答。", "What is photosynthesis?",
    "China 的气候有哪些特点？", "你好", "谢谢！", "", "   ",
])
def test_independent_questions_and_explicit_topic_switches_stay_independent(query):
    assert not needs_followup_context(query)


@pytest.mark.parametrize("query", [
    "那两个数字各指什么？", "这两种方案分别有什么限制？",
    "那三个参数应该怎样理解？", "这几步的先后顺序是什么？",
    "这12项分别对应哪些条件？",
])
def test_quantified_references_request_prior_topic_context(query):
    assert needs_followup_context(query)


def test_query_is_preserved_exactly_and_only_real_prior_user_questions_are_appended():
    query = "  它为什么会打滑？\n"
    previous = ["你好", "齿轮如何传递动力？", "同步带靠什么带动转轴？"]
    result = build_retrieval_query(query, previous)
    assert result.startswith(query)
    assert "前文提问（仅用于理解指代，不作为事实依据）" in result
    assert "齿轮如何传递动力？" in result
    assert "同步带靠什么带动转轴？" in result
    assert "你好" not in result
    assert previous == ["你好", "齿轮如何传递动力？", "同步带靠什么带动转轴？"]


def test_independent_question_never_carries_the_old_topic():
    query = "那望远镜呢？"
    assert build_retrieval_query(query, ["离心泵为什么会汽蚀？"]) == query


def test_only_the_latest_three_usable_questions_are_included_in_chronological_order():
    previous = ["水泵甲的原理？", "齿轮乙的原理？", "轴承丙的原理？", "皮带丁的原理？"]
    result = build_retrieval_query("它的限制呢？", previous)
    assert previous[0] not in result
    assert all(question in result for question in previous[-3:])
    assert result.index(previous[1]) < result.index(previous[2]) < result.index(previous[3])


def test_greetings_blanks_duplicates_and_empty_continuations_do_not_displace_a_topic_anchor():
    previous = ["电容为什么会储存能量？", "还有呢？", "继续", "谢谢！", "  ", "再具体说说"]
    result = build_retrieval_query("它有哪些用途？", previous)
    assert "电容为什么会储存能量？" in result
    assert "还有呢？" not in result
    assert "再具体说说" not in result
    one = build_retrieval_query("它有哪些用途？", ["电容为什么会储存能量？"] * 4)
    assert one.count("电容为什么会储存能量？") == 1


def test_budget_includes_the_whole_appended_block_and_never_slices_a_question():
    query = "它还有什么限制？"
    short = "惯性轮如何储能？"
    huge = "请解释超长的专有名称" + "连续完整术语" * 200 + "及其作用？"
    result = build_retrieval_query(query, [short, huge], max_history_chars=75)
    assert short in result and huge not in result
    assert "连续完整术语" not in result
    assert len(result) - len(query) <= 75


@pytest.mark.parametrize("budget", [0, -1, 1, 10, 30])
def test_too_small_budget_or_no_usable_history_returns_the_original_query(budget):
    query = "它的作用呢？"
    assert build_retrieval_query(query, ["离心泵的叶轮起什么作用？"], max_history_chars=budget) == query


@pytest.mark.parametrize("history", [[], ["", "  ", "你好", "谢谢"], ["继续", "还有呢？"]])
def test_no_anchor_does_not_fabricate_a_subject(history):
    assert build_retrieval_query("它是什么？", history) == "它是什么？"


def test_history_numbers_and_names_are_only_copied_not_completed_or_rewritten():
    previous = "陶瓷轴承XQ-27的检修周期怎么确定？"
    query = "刚才那个要几天？"
    result = build_retrieval_query(query, [previous])
    assert previous in result
    assert "30天" not in result and "7天" not in result
    assert result.endswith(previous)


def test_explicit_prior_topic_switch_stops_older_topic_pollution():
    previous = ["水泵为何会汽蚀？", "换个话题，太阳帆如何产生推力？", "还有呢？"]
    result = build_retrieval_query("它有什么限制？", previous)
    assert "太阳帆" in result
    assert "水泵" not in result


def test_oversize_topic_switch_does_not_fall_back_across_an_unrelated_old_topic():
    previous = ["水泵为何会汽蚀？", "换个话题，" + "光纤通信原理" * 80]
    assert build_retrieval_query("它有什么限制？", previous, 80) == "它有什么限制？"
