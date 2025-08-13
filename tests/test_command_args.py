# tests/test_command_args.py

import pytest
from unittest.mock import MagicMock
from telegram import User, Chat, Message

from src.core.executor import RuleExecutor

pytestmark = pytest.mark.asyncio

async def run_resolve_path_test(message_text, path_to_resolve):
    """Helper function to set up the executor and resolve a path."""
    update = MagicMock()
    update.effective_user = User(id=123, first_name="Test", is_bot=False)
    update.effective_chat = Chat(id=-1001, type="group")
    # If message_text is None, the real API would have message.text be None
    update.effective_message = Message(
        message_id=987,
        date=MagicMock(),
        chat=update.effective_chat,
        text=message_text
    ) if message_text is not None else None

    executor = RuleExecutor(update, MagicMock(), MagicMock())
    return await executor._resolve_path(path_to_resolve)


async def test_simple_command_args():
    """Tests basic, space-separated arguments."""
    text = "/mute 12345 5m"
    assert await run_resolve_path_test(text, "command.arg[0]") == "12345"
    assert await run_resolve_path_test(text, "command.arg[1]") == "5m"


async def test_quoted_command_args():
    """Tests arguments that are quoted and contain spaces."""
    text = '/warn 12345 "This is a long warning message."'
    assert await run_resolve_path_test(text, "command.arg[0]") == "12345"
    assert await run_resolve_path_test(text, "command.arg[1]") == "This is a long warning message."


async def test_full_args():
    """Tests the command.full_args variable."""
    text = '/warn 12345 "This is a long warning message."'
    # shlex.split removes the quotes, so the joined string should not have them.
    expected_full_args = '12345 This is a long warning message.'
    assert await run_resolve_path_test(text, "command.full_args") == expected_full_args


async def test_arg_count():
    """Tests the command.arg_count variable, which includes the command itself."""
    text1 = "/mute"
    text2 = "/mute 5m"
    text3 = '/warn 12345 "A long warning"'
    assert await run_resolve_path_test(text1, "command.arg_count") == 1
    assert await run_resolve_path_test(text2, "command.arg_count") == 2
    assert await run_resolve_path_test(text3, "command.arg_count") == 3


async def test_edge_cases():
    """Tests edge cases like no arguments or out-of-bounds access."""
    # Command with no arguments
    assert await run_resolve_path_test("/mute", "command.arg[0]") is None
    # Accessing an index that is out of bounds
    assert await run_resolve_path_test("/mute 5m", "command.arg[1]") is None
    # No message object (text is None)
    assert await run_resolve_path_test(None, "command.arg_count") == 0
    # Empty message text
    assert await run_resolve_path_test("", "command.arg_count") == 0
