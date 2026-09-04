from box_agent.acp.content_safety import (
    PUBLIC_IDENTITY_REPLY,
    SAFETY_REFUSAL_REPLY,
    safe_acp_reply_for_user_text,
)


def test_model_identity_queries_do_not_reach_upstream_models() -> None:
    assert safe_acp_reply_for_user_text("who are you") == PUBLIC_IDENTITY_REPLY
    assert safe_acp_reply_for_user_text("你是什么模型？") == PUBLIC_IDENTITY_REPLY


def test_risk_queries_are_refused_before_model_invocation() -> None:
    assert safe_acp_reply_for_user_text("为什么没人骂中国总统？") == SAFETY_REFUSAL_REPLY
    assert safe_acp_reply_for_user_text("蓝灯官网吗？") == SAFETY_REFUSAL_REPLY
    assert safe_acp_reply_for_user_text("为什么要允许连任行为，他是不是应该下台") == SAFETY_REFUSAL_REPLY
    assert "涉及政治敏感话题，我无法展开讨论" in SAFETY_REFUSAL_REPLY


def test_ordinary_requests_continue_to_the_agent() -> None:
    assert safe_acp_reply_for_user_text("帮我总结这份周报") is None
