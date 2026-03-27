import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from utils.checkin_executor import (
	BrowserCheckInError,
	apply_browser_headers,
	build_check_in_url,
	build_browser_cookies,
	detect_browser_login_required,
	execute_api_check_in,
	execute_check_in_action,
	execute_page_button_check_in_on_page,
	execute_page_challenge_check_in_on_page,
	inject_browser_local_storage,
	navigate_to_check_in_page,
	resolve_browser_check_in_url,
)
from utils.config import ProviderConfig
from checkin import get_user_info


class FakePage:
	def __init__(self):
		self.calls = []
		self.url = 'about:blank'
		self.title_text = 'Fake Page'
		self.body_text = ''
		self.goto_outcomes = []

	class FakeLocator:
		def __init__(self, page, selector):
			self.page = page
			self.selector = selector
			self.first = self

		async def count(self):
			return 1

		def nth(self, _index):
			return self

		async def is_visible(self):
			return True

		async def is_enabled(self):
			return True

		async def click(self, **_kwargs):
			self.page.calls.append(('locator_click', self.selector))

	async def wait_for_selector(self, selector, timeout=None):
		self.calls.append(('wait_for_selector', selector, timeout))

	async def click(self, selector):
		self.calls.append(('click', selector))

	async def select_option(self, selector, value):
		self.calls.append(('select_option', selector, value))

	async def fill(self, selector, value):
		self.calls.append(('fill', selector, value))

	async def wait_for_function(self, script, arg=None, timeout=None):
		self.calls.append(('wait_for_function', arg, timeout))

	async def wait_for_timeout(self, timeout):
		self.calls.append(('wait_for_timeout', timeout))

	async def wait_for_load_state(self, state):
		self.calls.append(('wait_for_load_state', state))

	async def goto(self, url, wait_until=None, timeout=None):
		self.calls.append(('goto', url, wait_until, timeout))
		if self.goto_outcomes:
			outcome = self.goto_outcomes.pop(0)
			if isinstance(outcome, Exception):
				raise outcome
			if isinstance(outcome, dict):
				self.url = outcome.get('url', self.url)
				return outcome

		self.url = url
		return {'url': url}

	async def title(self):
		return self.title_text

	async def evaluate(self, script, arg=None):
		self.calls.append(('evaluate', script, arg))
		if arg is None:
			return self.body_text
		if isinstance(arg, str):
			return arg in self.body_text
		if isinstance(arg, list):
			return any(text in self.body_text for text in arg)
		return False

	def locator(self, selector):
		return self.FakeLocator(self, selector)


def test_provider_config_infers_legacy_modes():
	api_provider = ProviderConfig(name='api', domain='https://example.com', sign_in_path='/api/sign')
	auto_provider = ProviderConfig(name='auto', domain='https://example.com', sign_in_path=None)

	assert api_provider.check_in_mode == 'api_post'
	assert auto_provider.check_in_mode == 'auto_user_info'


def test_build_check_in_url_uses_page_path():
	provider = ProviderConfig(
		name='page',
		domain='https://example.com',
		sign_in_path='/api/sign',
		check_in_mode='page_button',
		check_in_page_path='/mission/checkin',
	)

	assert build_check_in_url(provider) == 'https://example.com/mission/checkin'


def test_resolve_browser_check_in_url_supports_account_override():
	provider = ProviderConfig(
		name='page',
		domain='https://example.com',
		sign_in_path='/api/sign',
		check_in_mode='page_button',
		check_in_page_path='/mission/checkin',
	)

	result = resolve_browser_check_in_url(provider, 'https://widget.example.com/?token=abc')

	assert result == 'https://widget.example.com/?token=abc'


def test_detect_browser_login_required_by_page_text():
	page = FakePage()
	page.url = 'https://signv.ice.v.ua/'
	page.body_text = '通过 LinuxDO 登录后可进行签到与申请重置。 LinuxDO 登录'
	provider = ProviderConfig(
		name='signv_ice_v_ua',
		domain='https://signv.ice.v.ua',
		login_path='/login',
		sign_in_path=None,
		check_in_mode='page_button',
		check_in_page_path='/',
		check_in_config={'expired_texts': ['通过 LinuxDO 登录后可进行签到与申请重置。']},
	)

	reason = asyncio.run(detect_browser_login_required(page, provider))

	assert reason is not None
	assert 'login' in reason.lower()


def test_navigate_to_check_in_page_retries_after_about_blank_timeout():
	page = FakePage()
	page.goto_outcomes = [
		Exception('timeout'),
		{'url': 'https://checkin.9977.me/?token=abc'},
	]

	asyncio.run(navigate_to_check_in_page(page, 'https://checkin.9977.me/?token=abc', 'Account 1', attempts=2))

	goto_calls = [call for call in page.calls if call[0] == 'goto']
	assert len(goto_calls) == 2
	assert goto_calls[0][2] == 'domcontentloaded'
	assert goto_calls[1][2] == 'commit'


def test_navigate_to_check_in_page_raises_after_all_retries_fail():
	page = FakePage()
	page.goto_outcomes = [Exception('timeout'), Exception('timeout')]

	with pytest.raises(BrowserCheckInError):
		asyncio.run(navigate_to_check_in_page(page, 'https://checkin.9977.me/?token=abc', 'Account 1', attempts=2))


def test_build_browser_cookies_uses_url_binding():
	provider = ProviderConfig(name='page', domain='https://codesign.sakurapy.de', sign_in_path=None)

	result = build_browser_cookies(provider, {'ldp_session': 'token'})

	assert result == [{'name': 'ldp_session', 'value': 'token', 'url': 'https://codesign.sakurapy.de', 'httpOnly': False}]


def test_execute_api_check_in_success():
	request = httpx.Request('POST', 'https://example.com/api/sign')
	response = httpx.Response(200, request=request, json={'success': True})
	client = MagicMock()
	client.post.return_value = response

	provider = ProviderConfig(name='api', domain='https://example.com', sign_in_path='/api/sign', check_in_mode='api_post')
	success = execute_api_check_in(client, 'Account 1', provider, {'x-test': '1'})

	assert success is True
	client.post.assert_called_once()


def test_execute_page_button_flow_runs_pre_click_selectors_before_submit():
	page = FakePage()
	config = {
		'pre_click_selectors': [
			'button[role="tab"]:has-text("surprise")',
			'button.fortune-action',
		],
		'button_selector': 'button.confirm-check-in',
		'success_text': 'check-in success',
		'timeout_ms': 5000,
	}

	success = asyncio.run(execute_page_button_check_in_on_page(page, 'Account 1', config))

	assert success is True
	assert page.calls.count(('locator_click', 'button[role="tab"]:has-text("surprise")')) == 1
	assert page.calls.count(('locator_click', 'button.fortune-action')) == 1
	assert page.calls.count(('locator_click', 'button.confirm-check-in')) == 1
	assert page.calls.index(('locator_click', 'button[role="tab"]:has-text("surprise")')) < page.calls.index(
		('locator_click', 'button.confirm-check-in')
	)
	assert page.calls.index(('locator_click', 'button.fortune-action')) < page.calls.index(
		('locator_click', 'button.confirm-check-in')
	)


def test_execute_page_button_flow():
	page = FakePage()
	config = {
		'button_selector': 'button.check-in',
		'success_text': '签到成功',
		'timeout_ms': 5000,
	}

	success = asyncio.run(execute_page_button_check_in_on_page(page, 'Account 1', config))

	assert success is True
	assert ('locator_click', 'button.check-in') in page.calls
	assert ('wait_for_function', '签到成功', 5000) in page.calls


def test_execute_page_button_flow_supports_success_texts():
	page = FakePage()
	config = {
		'button_selector': 'button.check-in',
		'success_texts': ['签到成功', '已签到'],
		'timeout_ms': 5000,
	}

	success = asyncio.run(execute_page_button_check_in_on_page(page, 'Account 1', config))

	assert success is True
	assert ('locator_click', 'button.check-in') in page.calls
	assert ('wait_for_function', ['签到成功', '已签到'], 5000) in page.calls


def test_execute_page_challenge_flow():
	page = FakePage()
	config = {
		'difficulty_option_selector': '[data-level="easy"]',
		'start_button_selector': 'button.start',
		'submit_button_selector': 'button.submit',
		'ready_text': '计算完成',
		'success_text': '签到成功',
		'timeout_ms': 8000,
	}

	success = asyncio.run(execute_page_challenge_check_in_on_page(page, 'Account 1', config))

	assert success is True
	assert ('locator_click', '[data-level="easy"]') in page.calls
	assert ('locator_click', 'button.start') in page.calls
	assert ('locator_click', 'button.submit') in page.calls
	assert ('wait_for_function', '计算完成', 8000) in page.calls
	assert ('wait_for_function', '签到成功', 8000) in page.calls


def test_execute_check_in_action_dispatches_browser_mode():
	client = MagicMock()
	provider = ProviderConfig(
		name='page',
		domain='https://example.com',
		sign_in_path='/api/sign',
		check_in_mode='page_button',
		check_in_page_path='/mission/checkin',
		check_in_config={'button_selector': 'button.check-in'},
	)

	with patch('utils.checkin_executor.execute_browser_check_in', new=AsyncMock(return_value=True)) as mock_browser:
		success = asyncio.run(execute_check_in_action(client, 'Account 1', provider, {}, {'session': 'token'}))

	assert success is True
	mock_browser.assert_awaited_once_with('Account 1', provider, {'session': 'token'}, None, None, None)


def test_execute_check_in_action_dispatches_browser_mode_with_local_storage():
	client = MagicMock()
	provider = ProviderConfig(
		name='page',
		domain='https://example.com',
		sign_in_path='/api/sign',
		check_in_mode='page_button',
		check_in_page_path='/mission/checkin',
		check_in_config={'button_selector': 'button.check-in'},
	)

	with patch('utils.checkin_executor.execute_browser_check_in', new=AsyncMock(return_value=True)) as mock_browser:
		success = asyncio.run(
			execute_check_in_action(
				client,
				'Account 1',
				provider,
				{},
				{},
				{'welfare.session_token': 'jwt-token'},
			)
		)

	assert success is True
	mock_browser.assert_awaited_once_with('Account 1', provider, {}, {'welfare.session_token': 'jwt-token'}, None, None)


def test_execute_check_in_action_dispatches_browser_mode_with_headers():
	client = MagicMock()
	provider = ProviderConfig(
		name='page',
		domain='https://example.com',
		sign_in_path='/api/sign',
		check_in_mode='page_button',
		check_in_page_path='/mission/checkin',
		check_in_config={'button_selector': 'button.check-in'},
	)
	browser_headers = {'Authorization': 'Bearer test-token'}

	with patch('utils.checkin_executor.execute_browser_check_in', new=AsyncMock(return_value=True)) as mock_browser:
		success = asyncio.run(
			execute_check_in_action(
				client,
				'Account 1',
				provider,
				{},
				{},
				None,
				browser_headers,
			)
		)

	assert success is True
	mock_browser.assert_awaited_once_with('Account 1', provider, {}, None, browser_headers, None)


def test_execute_check_in_action_dispatches_browser_mode_with_check_in_url():
	client = MagicMock()
	provider = ProviderConfig(
		name='page',
		domain='https://example.com',
		sign_in_path='/api/sign',
		check_in_mode='page_button',
		check_in_page_path='/mission/checkin',
		check_in_config={'button_selector': 'button.check-in'},
	)
	browser_check_in_url = 'https://widget.example.com/?token=abc'

	with patch('utils.checkin_executor.execute_browser_check_in', new=AsyncMock(return_value=True)) as mock_browser:
		success = asyncio.run(
			execute_check_in_action(
				client,
				'Account 1',
				provider,
				{},
				{},
				None,
				None,
				browser_check_in_url,
			)
		)

	assert success is True
	mock_browser.assert_awaited_once_with('Account 1', provider, {}, None, None, browser_check_in_url)


def test_inject_browser_local_storage_uses_script_without_arg():
	context = AsyncMock()

	asyncio.run(inject_browser_local_storage(context, {'welfare.session_token': 'jwt-token'}))

	context.add_init_script.assert_awaited_once()
	kwargs = context.add_init_script.await_args.kwargs
	assert 'script' in kwargs
	assert 'welfare.session_token' in kwargs['script']
	assert 'jwt-token' in kwargs['script']


def test_apply_browser_headers_sets_extra_http_headers():
	context = AsyncMock()
	headers = {'Authorization': 'Bearer test-token'}

	asyncio.run(apply_browser_headers(context, headers))

	context.set_extra_http_headers.assert_awaited_once_with(headers)


def test_get_user_info_supports_sign_status_mode():
	request = httpx.Request('GET', 'https://sign.qaq.al/api/me')
	response = httpx.Response(200, request=request, json={'signedInToday': True, 'user': {'name': 'tester'}})
	client = MagicMock()
	client.get.return_value = response
	provider = ProviderConfig(
		name='sign_qaq_al',
		domain='https://sign.qaq.al',
		sign_in_path=None,
		user_info_path='/api/me',
		user_info_mode='sign_status',
		user_info_success_field='signedInToday',
		check_in_mode='page_challenge',
	)

	result = get_user_info(client, {}, 'https://sign.qaq.al/api/me', provider)

	assert result['success'] is True
	assert 'Signed in today' in result['display']


def test_get_user_info_supports_record_status_mode():
	request = httpx.Request('GET', 'https://codesign.sakurapy.de/api/sign/bootstrap')
	response = httpx.Response(
		200,
		request=request,
		json={'today_record': {'difficulty_key': 'extreme', 'reward_amount': '18.80'}},
	)
	client = MagicMock()
	client.get.return_value = response
	provider = ProviderConfig(
		name='codesign_sakurapy_de',
		domain='https://codesign.sakurapy.de',
		sign_in_path=None,
		user_info_path='/api/sign/bootstrap',
		user_info_mode='record_status',
		user_info_success_field='today_record',
		check_in_mode='page_challenge',
	)

	result = get_user_info(client, {}, 'https://codesign.sakurapy.de/api/sign/bootstrap', provider)

	assert result['success'] is True
	assert 'Today record exists: yes' in result['display']
	assert 'difficulty=extreme' in result['display']


def test_sign_status_mode_should_skip_manual_flow_when_already_signed():
	from checkin import check_in_account
	from utils.config import AccountConfig, AppConfig

	provider = ProviderConfig(
		name='sign_qaq_al',
		domain='https://sign.qaq.al',
		sign_in_path=None,
		user_info_path='/api/me',
		user_info_mode='sign_status',
		user_info_success_field='signedInToday',
		check_in_mode='page_challenge',
		check_in_page_path='/app',
	)
	account = AccountConfig(cookies={'sid': 'test'}, api_user='0', provider='sign_qaq_al', name='sign-qaq-al')
	app_config = AppConfig(providers={'sign_qaq_al': provider})

	request = httpx.Request('GET', 'https://sign.qaq.al/api/me')
	response = httpx.Response(200, request=request, json={'signedInToday': True})

	with (
		patch('checkin.prepare_cookies', new=AsyncMock(return_value={'sid': 'test'})),
		patch('checkin.httpx.Client') as mock_client_class,
		patch('checkin.execute_check_in_action', new=AsyncMock(return_value=False)) as mock_execute,
	):
		mock_client = MagicMock()
		mock_client.get.return_value = response
		mock_client_class.return_value = mock_client

		success, user_info = asyncio.run(check_in_account(account, 0, app_config))

	assert success is True
	assert user_info['success'] is True
	mock_execute.assert_not_awaited()


def test_record_status_mode_should_skip_manual_flow_when_today_record_exists():
	from checkin import check_in_account
	from utils.config import AccountConfig, AppConfig

	provider = ProviderConfig(
		name='codesign_sakurapy_de',
		domain='https://codesign.sakurapy.de',
		sign_in_path=None,
		user_info_path='/api/sign/bootstrap',
		user_info_mode='record_status',
		user_info_success_field='today_record',
		check_in_mode='page_challenge',
		check_in_page_path='/sign',
	)
	account = AccountConfig(
		cookies={'ldp_session': 'test'},
		api_user='0',
		provider='codesign_sakurapy_de',
		name='codesign-sakurapy-de',
	)
	app_config = AppConfig(providers={'codesign_sakurapy_de': provider})

	request = httpx.Request('GET', 'https://codesign.sakurapy.de/api/sign/bootstrap')
	response = httpx.Response(200, request=request, json={'today_record': {'difficulty_key': 'extreme'}})

	with (
		patch('checkin.prepare_cookies', new=AsyncMock(return_value={'ldp_session': 'test'})),
		patch('checkin.httpx.Client') as mock_client_class,
		patch('checkin.execute_check_in_action', new=AsyncMock(return_value=False)) as mock_execute,
	):
		mock_client = MagicMock()
		mock_client.get.return_value = response
		mock_client_class.return_value = mock_client

		success, user_info = asyncio.run(check_in_account(account, 0, app_config))

	assert success is True
	assert user_info['success'] is True
	mock_execute.assert_not_awaited()


def test_check_in_account_allows_browser_local_storage_without_cookies():
	from checkin import check_in_account
	from utils.config import AccountConfig, AppConfig

	provider = ProviderConfig(
		name='welfare_frontend_zeabur_app',
		domain='https://welfare-frontend.zeabur.app',
		sign_in_path=None,
		user_info_mode='none',
		check_in_mode='page_button',
		check_in_page_path='/checkin',
		check_in_config={'button_selector': 'button:has-text("立即签到")'},
	)
	account = AccountConfig(
		cookies={},
		api_user='0',
		provider='welfare_frontend_zeabur_app',
		name='welfare',
		browser_local_storage={'welfare.session_token': 'jwt-token'},
	)
	app_config = AppConfig(providers={'welfare_frontend_zeabur_app': provider})

	with (
		patch('checkin.prepare_cookies', new=AsyncMock(return_value={})),
		patch('checkin.httpx.Client') as mock_client_class,
		patch('checkin.execute_check_in_action', new=AsyncMock(return_value=True)) as mock_execute,
	):
		mock_client = MagicMock()
		mock_client_class.return_value = mock_client

		success, user_info = asyncio.run(check_in_account(account, 0, app_config))

	assert success is True
	assert user_info is None
	mock_execute.assert_awaited_once()
	args = mock_execute.await_args.args
	assert args[0] is mock_client
	assert args[1] == 'welfare'
	assert args[2] == provider
	assert args[4] == {}
	assert args[5] == {'welfare.session_token': 'jwt-token'}
	assert args[6] is None
	assert args[7] is None


def test_check_in_account_allows_browser_headers_without_cookies():
	from checkin import check_in_account
	from utils.config import AccountConfig, AppConfig

	provider = ProviderConfig(
		name='free_9977_me',
		domain='https://free.9977.me',
		sign_in_path=None,
		user_info_mode='none',
		check_in_mode='page_button',
		check_in_page_path='/purchase',
		check_in_config={'button_selector': '#btn-claim'},
	)
	account = AccountConfig(
		cookies={},
		api_user='0',
		provider='free_9977_me',
		name='free-9977-me',
		browser_headers={'Authorization': 'Bearer test-token'},
	)
	app_config = AppConfig(providers={'free_9977_me': provider})

	with (
		patch('checkin.prepare_cookies', new=AsyncMock(return_value={})),
		patch('checkin.httpx.Client') as mock_client_class,
		patch('checkin.execute_check_in_action', new=AsyncMock(return_value=True)) as mock_execute,
	):
		mock_client = MagicMock()
		mock_client_class.return_value = mock_client

		success, user_info = asyncio.run(check_in_account(account, 0, app_config))

	assert success is True
	assert user_info is None
	mock_execute.assert_awaited_once()
	args = mock_execute.await_args.args
	assert args[0] is mock_client
	assert args[1] == 'free-9977-me'
	assert args[2] == provider
	assert args[4] == {}
	assert args[5] is None
	assert args[6] == {'Authorization': 'Bearer test-token'}


def test_check_in_account_allows_browser_check_in_url_without_cookies():
	from checkin import check_in_account
	from utils.config import AccountConfig, AppConfig

	provider = ProviderConfig(
		name='free_9977_me',
		domain='https://free.9977.me',
		sign_in_path=None,
		user_info_mode='none',
		check_in_mode='page_button',
		check_in_page_path='/purchase',
		check_in_config={'button_selector': '#btn-claim'},
	)
	account = AccountConfig(
		cookies={},
		api_user='0',
		provider='free_9977_me',
		name='free_9977_me',
		browser_check_in_url='https://checkin.9977.me/?token=abc',
	)
	app_config = AppConfig(providers={'free_9977_me': provider})

	with (
		patch('checkin.prepare_cookies', new=AsyncMock(return_value={})),
		patch('checkin.httpx.Client') as mock_client_class,
		patch('checkin.execute_check_in_action', new=AsyncMock(return_value=True)) as mock_execute,
	):
		mock_client = MagicMock()
		mock_client_class.return_value = mock_client

		success, user_info = asyncio.run(check_in_account(account, 0, app_config))

	assert success is True
	assert user_info is None
	mock_execute.assert_awaited_once()
	args = mock_execute.await_args.args
	assert args[0] is mock_client
	assert args[1] == 'free_9977_me'
	assert args[2] == provider
	assert args[4] == {}
	assert args[5] is None
	assert args[6] is None
	assert args[7] == 'https://checkin.9977.me/?token=abc'
