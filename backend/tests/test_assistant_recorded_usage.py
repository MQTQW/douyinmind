from app.assistant.legacy_bridge import recorded_usage


def response(usage):
    return {'kind':'model_response','metadata':{'usage':usage}}


def test_no_responses_have_no_claimed_token_count():
    assert recorded_usage([], elapsed_ms=19) == {'model_responses':0,'elapsed_ms':19}


def test_missing_or_partial_usage_never_becomes_zero_total():
    assert recorded_usage([response({})]) == {'model_responses':1}
    result = recorded_usage([response({'prompt_tokens':4,'completion_tokens':2,'total_tokens':6}),
                             response({'prompt_tokens':3})])
    assert result['input_tokens'] == 7 and result['model_responses'] == 2
    assert 'total_tokens' not in result and 'output_tokens' not in result


def test_explicit_zero_and_alternative_provider_names_are_preserved():
    result = recorded_usage([response({'input_tokens':4,'output_tokens':0,'total_tokens':4}),
                             response({'prompt_tokens':0,'completion_tokens':2,'total_tokens':2})])
    assert result == {'input_tokens':4,'output_tokens':2,'total_tokens':6,'model_responses':2}


def test_invalid_values_are_not_reported_as_measured_usage():
    for value in [None,True,-1,'4',float('inf')]:
        result = recorded_usage([response({'total_tokens':value})])
        assert 'total_tokens' not in result
