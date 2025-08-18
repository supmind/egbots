# tests/test_utils.py
import pytest
import io
from unittest.mock import MagicMock, AsyncMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError

from telegram import ChatPermissions

from src.utils import session_scope, generate_math_image, unmute_user_util

# =================== Fixtures ===================

@pytest.fixture(scope="function")
def memory_db_session_factory():
    """
    一个 Pytest Fixture，用于创建一个基于内存的 SQLite 数据库。
    它使用 StaticPool 来确保所有连接都指向同一个内存实例，
    这对于测试事务行为至关重要。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # 创建表结构
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT UNIQUE)"))

    session_factory = sessionmaker(bind=engine)
    yield session_factory
    engine.dispose()


# =================== 测试 session_scope ===================

def test_session_scope_commits_on_success(memory_db_session_factory):
    """
    测试: 验证 session_scope 在没有异常时会成功提交事务。
    """
    # 在 session_scope 内插入数据
    with session_scope(memory_db_session_factory) as session:
        session.execute(text("INSERT INTO test (id, data) VALUES (1, 'test_data')"))

    # 在一个新的会话中验证数据是否已提交
    with session_scope(memory_db_session_factory) as session:
        result = session.execute(text("SELECT data FROM test WHERE id = 1")).scalar_one_or_none()
        assert result == "test_data"

def test_session_scope_rolls_back_on_exception(memory_db_session_factory):
    """
    测试: 验证 session_scope 在遇到异常时会回滚事务。
    """
    # 先插入一条基准数据
    with session_scope(memory_db_session_factory) as session:
        session.execute(text("INSERT INTO test (id, data) VALUES (1, 'existing')"))

    # 尝试插入一个重复的数据，这会引发 IntegrityError
    with pytest.raises(IntegrityError):
        with session_scope(memory_db_session_factory) as session:
            session.execute(text("INSERT INTO test (id, data) VALUES (2, 'new_data')"))
            # 这是一个会失败的操作，因为 'existing' 已经存在且 UNIQUE
            session.execute(text("INSERT INTO test (id, data) VALUES (3, 'existing')"))
            # 调用 flush() 会在 commit 前触发数据库的约束检查
            session.flush()

    # 验证'new_data'也没有被提交，证明整个事务被回滚了
    with session_scope(memory_db_session_factory) as session:
        result = session.execute(text("SELECT data FROM test WHERE id = 2")).scalar_one_or_none()
        assert result is None

# =================== 测试 generate_math_image ===================

def test_generate_math_image_returns_valid_png():
    """
    测试: 验证 generate_math_image 能生成一个有效的 PNG 图片。
    """
    problem = "2 + 2 = ?"
    image_bytes = generate_math_image(problem)

    # 验证返回的是一个 BytesIO 对象
    assert isinstance(image_bytes, io.BytesIO)
    # 验证文件头是 PNG 的魔术数字
    assert image_bytes.getvalue().startswith(b'\x89PNG\r\n\x1a\n')

@patch('src.utils.ImageDraw.Draw')
@patch('src.utils.ImageFont.truetype')
@patch('src.utils.ImageFont.load_default')
def test_generate_math_image_font_fallback(mock_load_default, mock_truetype, mock_draw):
    """
    测试: 验证当首选字体加载失败时，generate_math_image 会回退到使用默认字体。
    """
    # 模拟加载首选字体时抛出 IOError
    mock_truetype.side_effect = IOError("Font not found")

    problem = "5 + 5 = ?"
    generate_math_image(problem)

    # 验证是否尝试加载了首选字体
    mock_truetype.assert_called_once()
    # 验证是否因失败而调用了回退的默认字体加载
    mock_load_default.assert_called_once()
    # 验证 ImageDraw.Draw 被调用，证明函数的核心逻辑在继续
    mock_draw.assert_called_once()


# =================== 测试 unmute_user_util ===================

@pytest.mark.asyncio
async def test_unmute_user_util_with_specific_permissions(mock_context):
    """
    测试: 验证 unmute_user_util 在获取到群组特定权限时，能使用这些权限来解禁用户。
    """
    chat_id = -1001
    user_id = 123
    # 模拟一个独特的权限对象，以确保测试的准确性
    mock_permissions = ChatPermissions(can_send_messages=True, can_invite_users=False)
    mock_chat = MagicMock()
    mock_chat.permissions = mock_permissions

    # 配置 bot 的 get_chat 方法返回我们的模拟对象
    mock_context.bot.get_chat = AsyncMock(return_value=mock_chat)

    result = await unmute_user_util(mock_context, chat_id, user_id)

    # 验证操作成功
    assert result is True
    # 验证 get_chat 被调用以获取权限
    mock_context.bot.get_chat.assert_called_once_with(chat_id=chat_id)
    # 验证 restrict_chat_member 被调用，并且使用的是我们模拟的特定权限
    mock_context.bot.restrict_chat_member.assert_called_once_with(
        chat_id=chat_id,
        user_id=user_id,
        permissions=mock_permissions
    )

@pytest.mark.asyncio
async def test_unmute_user_util_with_default_fallback_permissions(mock_context):
    """
    测试: 验证当 get_chat 未返回权限时，unmute_user_util 会使用一套理智的默认权限。
    """
    chat_id = -1001
    user_id = 123
    # 模拟 get_chat 返回一个没有 permissions 属性的 Chat 对象
    mock_chat = MagicMock()
    mock_chat.permissions = None
    mock_context.bot.get_chat = AsyncMock(return_value=mock_chat)

    result = await unmute_user_util(mock_context, chat_id, user_id)

    # 验证操作成功
    assert result is True
    # 验证 restrict_chat_member 被调用
    mock_context.bot.restrict_chat_member.assert_called_once()
    # 验证使用的权限是回退的默认权限（检查几个关键权限）
    _, kwargs = mock_context.bot.restrict_chat_member.call_args
    assert kwargs['permissions'].can_send_messages is True
    assert kwargs['permissions'].can_add_web_page_previews is True

@pytest.mark.asyncio
async def test_unmute_user_util_handles_exception(mock_context):
    """
    测试: 验证当 bot API 调用失败时，unmute_user_util 能捕获异常并返回 False。
    """
    chat_id = -1001
    user_id = 123
    # 模拟 get_chat 调用（因为 unmute 会先调用它）
    mock_context.bot.get_chat = AsyncMock(return_value=MagicMock())
    # 模拟 restrict_chat_member 调用时抛出异常
    mock_context.bot.restrict_chat_member.side_effect = Exception("API call failed")

    result = await unmute_user_util(mock_context, chat_id, user_id)

    # 验证操作失败
    assert result is False
