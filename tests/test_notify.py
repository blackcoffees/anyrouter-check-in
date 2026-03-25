import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

load_dotenv(project_root / '.env')

from utils.notify import NotificationKit


@pytest.fixture
def notification_kit(monkeypatch):
	monkeypatch.setenv('EMAIL_USER', 'sender@example.com')
	monkeypatch.setenv('EMAIL_PASS', 'password')
	monkeypatch.setenv('EMAIL_TO', 'receiver@example.com')
	monkeypatch.setenv('PUSHPLUS_TOKEN', 'test_token')
	monkeypatch.setenv('DINGDING_WEBHOOK', 'https://oapi.dingtalk.com/robot/send?access_token=test')
	monkeypatch.setenv('FEISHU_WEBHOOK', 'https://open.feishu.cn/open-apis/bot/v2/hook/test')
	monkeypatch.setenv('WEIXIN_WEBHOOK', 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test')
	monkeypatch.setenv('GOTIFY_URL', 'https://gotify.example.com/message')
	monkeypatch.setenv('GOTIFY_TOKEN', 'test_token')
	monkeypatch.setenv('GOTIFY_PRIORITY', '9')
	return NotificationKit()


@patch('smtplib.SMTP_SSL')
def test_send_email(mock_smtp, notification_kit):
	mock_server = MagicMock()
	mock_smtp.return_value.__enter__.return_value = mock_server

	notification_kit.send_email('测试标题', '测试内容')

	assert mock_server.login.called
	assert mock_server.send_message.called


@patch('httpx.Client')
def test_send_pushplus(mock_client_class, notification_kit):
	mock_client = MagicMock()
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_pushplus('测试标题', '测试内容')

	mock_client.post.assert_called_once()
	assert 'pushplus' in mock_client.post.call_args.args[0]
	assert mock_client.post.call_args.kwargs['json']['token'] == 'test_token'


@patch('httpx.Client')
def test_send_dingtalk(mock_client_class, notification_kit):
	mock_client = MagicMock()
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_dingtalk('测试标题', '测试内容')

	mock_client.post.assert_called_once_with(
		'https://oapi.dingtalk.com/robot/send?access_token=test',
		json={'msgtype': 'text', 'text': {'content': '测试标题\n测试内容'}},
	)


@patch('httpx.Client')
def test_send_feishu(mock_client_class, notification_kit):
	mock_client = MagicMock()
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_feishu('测试标题', '测试内容')

	mock_client.post.assert_called_once()
	assert 'card' in mock_client.post.call_args.kwargs['json']


@patch('httpx.Client')
def test_send_wecom(mock_client_class, notification_kit):
	mock_client = MagicMock()
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_wecom('测试标题', '测试内容')

	mock_client.post.assert_called_once_with(
		'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test',
		json={'msgtype': 'text', 'text': {'content': '测试标题\n测试内容'}},
	)


@patch('httpx.Client')
def test_send_gotify(mock_client_class, notification_kit):
	mock_client = MagicMock()
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_gotify('测试标题', '测试内容')

	mock_client.post.assert_called_once_with(
		'https://gotify.example.com/message?token=test_token',
		json={'title': '测试标题', 'message': '测试内容', 'priority': 9},
	)


def test_missing_config(monkeypatch):
	for key in [
		'EMAIL_USER',
		'EMAIL_PASS',
		'EMAIL_TO',
		'PUSHPLUS_TOKEN',
		'DINGDING_WEBHOOK',
		'FEISHU_WEBHOOK',
		'WEIXIN_WEBHOOK',
		'GOTIFY_URL',
		'GOTIFY_TOKEN',
	]:
		monkeypatch.delenv(key, raising=False)

	kit = NotificationKit()

	with pytest.raises(ValueError, match='Email configuration not set'):
		kit.send_email('测试', '测试')

	with pytest.raises(ValueError, match='PushPlus Token not configured'):
		kit.send_pushplus('测试', '测试')


def test_push_message(notification_kit):
	with (
		patch.object(NotificationKit, 'send_email') as mock_email,
		patch.object(NotificationKit, 'send_dingtalk') as mock_dingtalk,
		patch.object(NotificationKit, 'send_wecom') as mock_wecom,
		patch.object(NotificationKit, 'send_pushplus') as mock_pushplus,
		patch.object(NotificationKit, 'send_feishu') as mock_feishu,
		patch.object(NotificationKit, 'send_gotify') as mock_gotify,
		patch.object(NotificationKit, 'send_serverPush') as mock_server_push,
		patch.object(NotificationKit, 'send_telegram') as mock_telegram,
	):
		notification_kit.push_message('测试标题', '测试内容')

	assert mock_email.called
	assert mock_dingtalk.called
	assert mock_wecom.called
	assert mock_pushplus.called
	assert mock_feishu.called
	assert mock_gotify.called
	assert mock_server_push.called
	assert mock_telegram.called
