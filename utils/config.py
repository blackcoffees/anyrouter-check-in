#!/usr/bin/env python3
"""
配置管理模块
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Literal

CheckInMode = Literal['api_post', 'auto_user_info', 'page_button', 'page_challenge']
UserInfoMode = Literal['quota', 'sign_status', 'record_status', 'none']


@dataclass
class ProviderConfig:
	"""Provider 配置"""

	name: str
	domain: str
	login_path: str = '/login'
	sign_in_path: str | None = '/api/user/sign_in'
	user_info_path: str = '/api/user/self'
	user_info_mode: UserInfoMode = 'quota'
	user_info_success_field: str | None = None
	api_user_key: str = 'new-api-user'
	bypass_method: Literal['waf_cookies'] | None = None
	waf_cookie_names: List[str] | None = None
	check_in_mode: CheckInMode | None = None
	check_in_page_path: str | None = None
	check_in_config: Dict[str, Any] | None = None

	def __post_init__(self):
		required_waf_cookies = set()
		if self.waf_cookie_names and isinstance(self.waf_cookie_names, list):
			for item in self.waf_cookie_names:
				name = '' if not item or not isinstance(item, str) else item.strip()
				if not name:
					print(f'[WARNING] Found invalid WAF cookie name: {item}')
					continue

				required_waf_cookies.add(name)

		if not required_waf_cookies:
			self.bypass_method = None

		self.waf_cookie_names = list(required_waf_cookies)

		if isinstance(self.sign_in_path, str):
			self.sign_in_path = self.sign_in_path.strip() or None

		if isinstance(self.check_in_page_path, str):
			self.check_in_page_path = self.check_in_page_path.strip() or None

		if self.check_in_mode is None:
			self.check_in_mode = 'auto_user_info' if not self.sign_in_path else 'api_post'

		if self.check_in_mode not in ('api_post', 'auto_user_info', 'page_button', 'page_challenge'):
			raise ValueError(f'Unsupported check_in_mode: {self.check_in_mode}')

		if self.user_info_mode not in ('quota', 'sign_status', 'record_status', 'none'):
			raise ValueError(f'Unsupported user_info_mode: {self.user_info_mode}')

		if self.check_in_config is None:
			self.check_in_config = {}
		elif not isinstance(self.check_in_config, dict):
			raise ValueError('check_in_config must be a JSON object')

		if self.check_in_mode == 'api_post' and not self.sign_in_path:
			raise ValueError('sign_in_path is required when check_in_mode is api_post')

	def needs_waf_cookies(self) -> bool:
		"""判断是否需要获取 WAF cookies"""
		return self.bypass_method == 'waf_cookies'

	def needs_manual_check_in(self) -> bool:
		"""判断是否需要执行显式签到动作"""
		return self.check_in_mode != 'auto_user_info'

	def requires_browser_check_in(self) -> bool:
		"""判断是否需要使用 Playwright 执行页面签到"""
		return self.check_in_mode in ('page_button', 'page_challenge')

	@classmethod
	def from_dict(cls, name: str, data: dict) -> 'ProviderConfig':
		"""从字典创建 ProviderConfig"""
		return cls(
			name=name,
			domain=data['domain'],
			login_path=data.get('login_path', '/login'),
			sign_in_path=data.get('sign_in_path', '/api/user/sign_in'),
			user_info_path=data.get('user_info_path', '/api/user/self'),
			user_info_mode=data.get('user_info_mode', 'quota'),
			user_info_success_field=data.get('user_info_success_field'),
			api_user_key=data.get('api_user_key', 'new-api-user'),
			bypass_method=data.get('bypass_method'),
			waf_cookie_names=data.get('waf_cookie_names'),
			check_in_mode=data.get('check_in_mode'),
			check_in_page_path=data.get('check_in_page_path'),
			check_in_config=data.get('check_in_config'),
		)


@dataclass
class AppConfig:
	"""应用配置"""

	providers: Dict[str, ProviderConfig]

	@classmethod
	def load_from_env(cls) -> 'AppConfig':
		"""从环境变量加载配置"""
		providers = {
			'anyrouter': ProviderConfig(
				name='anyrouter',
				domain='https://anyrouter.top',
				login_path='/login',
				sign_in_path='/api/user/sign_in',
				user_info_path='/api/user/self',
				user_info_mode='quota',
				api_user_key='new-api-user',
				bypass_method='waf_cookies',
				waf_cookie_names=['acw_tc', 'cdn_sec_tc', 'acw_sc__v2'],
				check_in_mode='api_post',
			),
			'agentrouter': ProviderConfig(
				name='agentrouter',
				domain='https://agentrouter.org',
				login_path='/login',
				sign_in_path=None,
				user_info_path='/api/user/self',
				user_info_mode='quota',
				api_user_key='new-api-user',
				bypass_method='waf_cookies',
				waf_cookie_names=['acw_tc'],
				check_in_mode='auto_user_info',
			),
		}

		providers_str = os.getenv('PROVIDERS')
		if providers_str:
			try:
				providers_data = json.loads(providers_str)

				if not isinstance(providers_data, dict):
					print('[WARNING] PROVIDERS must be a JSON object, ignoring custom providers')
					return cls(providers=providers)

				for name, provider_data in providers_data.items():
					try:
						providers[name] = ProviderConfig.from_dict(name, provider_data)
					except Exception as e:
						print(f'[WARNING] Failed to parse provider "{name}": {e}, skipping')
						continue

				print(f'[INFO] Loaded {len(providers_data)} custom provider(s) from PROVIDERS environment variable')
			except json.JSONDecodeError as e:
				print(f'[WARNING] Failed to parse PROVIDERS environment variable: {e}, using default configuration only')
			except Exception as e:
				print(f'[WARNING] Error loading PROVIDERS: {e}, using default configuration only')

		return cls(providers=providers)

	def get_provider(self, name: str) -> ProviderConfig | None:
		"""获取指定 provider 配置"""
		return self.providers.get(name)


@dataclass
class AccountConfig:
	"""账号配置"""

	cookies: dict | str
	api_user: str
	provider: str = 'anyrouter'
	name: str | None = None
	browser_local_storage: dict[str, str] | None = None
	browser_headers: dict[str, str] | None = None
	browser_check_in_url: str | None = None

	@classmethod
	def from_dict(cls, data: dict, index: int) -> 'AccountConfig':
		"""从字典创建 AccountConfig"""
		provider = data.get('provider', 'anyrouter')
		name = data.get('name', f'Account {index + 1}')

		return cls(
			cookies=data['cookies'],
			api_user=data['api_user'],
			provider=provider,
			name=name if name else None,
			browser_local_storage=data.get('browser_local_storage'),
			browser_headers=data.get('browser_headers'),
			browser_check_in_url=data.get('browser_check_in_url'),
		)

	def get_display_name(self, index: int) -> str:
		"""获取显示名称"""
		return self.name if self.name else f'Account {index + 1}'


def load_accounts_config() -> list[AccountConfig] | None:
	"""从环境变量加载账号配置"""
	accounts_str = os.getenv('ANYROUTER_ACCOUNTS')
	if not accounts_str:
		print('ERROR: ANYROUTER_ACCOUNTS environment variable not found')
		return None

	try:
		accounts_data = json.loads(accounts_str)

		if not isinstance(accounts_data, list):
			print('ERROR: Account configuration must use array format [{}]')
			return None

		accounts = []
		for i, account_dict in enumerate(accounts_data):
			if not isinstance(account_dict, dict):
				print(f'ERROR: Account {i + 1} configuration format is incorrect')
				return None

			if 'cookies' not in account_dict or 'api_user' not in account_dict:
				print(f'ERROR: Account {i + 1} missing required fields (cookies, api_user)')
				return None

			if 'name' in account_dict and not account_dict['name']:
				print(f'ERROR: Account {i + 1} name field cannot be empty')
				return None

			accounts.append(AccountConfig.from_dict(account_dict, i))

		return accounts
	except Exception as e:
		print(f'ERROR: Account configuration format is incorrect: {e}')
		return None
