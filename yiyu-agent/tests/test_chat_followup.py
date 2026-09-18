"""追问从会话记录进入回答 Prompt；离线、不调用模型。"""
import asyncio
import importlib
from types import SimpleNamespace

from runtime.assembler import PromptAssembler
from runtime.events import AgentEvent, EventType
from runtime.state import AgentState
from store.repos.conversation_repo import ConversationRepo


def test_followup_history_reaches_answer_model_and_stays_tenant_scoped(tmp_path, monkeypatch):
    chat = importlib.import_module('api.routes.chat')
    repo = ConversationRepo(f'sqlite:///{tmp_path / "history.db"}')
    repo.ensure('company-chat', 'owner', '分析智谱')
    repo.add_message('company-chat', 'owner', 'user', '分析智谱')
    repo.add_message('company-chat', 'owner', 'assistant', '智谱的市净率是100倍，需要核实口径。')
    monkeypatch.setattr(chat, '_conversation_repo', lambda: repo)
    monkeypatch.setattr(chat.settings, 'trace_enabled', False)
    import core.rate_limit
    monkeypatch.setattr(core.rate_limit, 'get_rate_limiter', lambda: SimpleNamespace(
        check_and_incr=lambda _: (True, 1),
    ))
    prompts = []
    routed_histories = []

    async def route_async(**kwargs):
        routed_histories.append(kwargs['history'])
        return SimpleNamespace(skill='knowledge_qa', route_result='light_answer', matched_by='test')

    monkeypatch.setattr(chat, 'route_async', route_async)

    class Loop:
        async def run(self, *, user_message, session_id, initial_context, **kwargs):
            state = AgentState(session_id=session_id, user_message=user_message, context=initial_context)
            prompts.append(await PromptAssembler().build(state))
            yield AgentEvent(type=EventType.FINAL_ANSWER, content='这里需要核查之前引用的市净率。')

    async def ask(sid, uid, question):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            agent_loop=Loop(), llm_client=None,
        )))
        response = await chat.chat_endpoint(chat.ChatRequest(message=question, session_id=sid),
                                            request, {'user_id': uid})
        return [event async for event in response.body_iterator]

    asyncio.run(ask('company-chat', 'owner', '为什么是100倍市净率？'))
    assert '分析智谱' in prompts[-1].user
    assert '智谱的市净率是100倍' in prompts[-1].user
    assert prompts[-1].user.endswith('本次问题：\n为什么是100倍市净率？')
    assert len(routed_histories[-1]) == 2  # 当前追问没有重复塞进历史
    assert '历史回答不等于已核验事实' in prompts[-1].user

    asyncio.run(ask('company-chat', 'owner', '换成分析贵州茅台'))
    assert prompts[-1].user.endswith('本次问题：\n换成分析贵州茅台')
    assert '明确换公司或换话题' in prompts[-1].user

    asyncio.run(ask('fresh-chat', 'owner', '为什么是100倍市净率？'))
    assert prompts[-1].user == '为什么是100倍市净率？'
    asyncio.run(ask('company-chat', 'other-user', '为什么是100倍市净率？'))
    assert prompts[-1].user == '为什么是100倍市净率？'


def test_history_is_bounded_and_available_during_synthesis():
    state = AgentState(session_id='bounded', user_message='核查这个数字', context={
        '_prompt_stage': 'synthesis',
        'conversation_history': [{'role': 'user', 'content': '旧话题不应进入'}] + [
            {'role': 'assistant', 'content': '智谱 ' + 'x' * 10000} for _ in range(6)
        ],
    })
    prompt = asyncio.run(PromptAssembler().build(state))
    assert '旧话题不应进入' not in prompt.user
    assert '智谱' in prompt.user
    assert len(prompt.user) < 25000
    assert prompt.breakdown['user'] == len(prompt.user)
