import asyncio
import logging
import aiohttp

from ..config import ConfigObject
from .accesslog import AccessLogger

class WebContainer(ConfigObject):


	ConfigDefaults = {
		'listen': '0.0.0.0 8080', # Can be multiline
		'rootdir': '',
		'servertokens': 'full' # Controls whether 'Server' response header field is included ('full') or faked 'prod' ()
	}


	def __init__(self, websvc, config_section_name, config=None):
		super().__init__(config_section_name=config_section_name, config=config)

		servertokens = self.Config.get("servertokens")
		if servertokens == 'prod':
			# Because we cannot remove token completely
			self.ServerTokens = "asab"
		else:
			from .. import __version__
			self.ServerTokens = aiohttp.web_response.SERVER_SOFTWARE + " asab/" + __version__

		# Parse listen address(es), can be multiline configuration item
		ls = self.Config.get("listen")
		self._listen = []
		for l in ls.split('\n'):
			addr, port = l.split(' ', 1)
			port = int(port)
			self._listen.append((addr, port))

		self.WebApp = aiohttp.web.Application(loop=websvc.App.Loop)
		self.WebApp.on_response_prepare.append(self._on_prepare_response)
		self.WebApp['app'] = websvc.App

		rootdir = self.Config.get("rootdir")
		if len(rootdir) > 0:
			from .staticdir import StaticDirProvider
			self.WebApp['rootdir'] = StaticDirProvider(self.WebApp, root='/', path=rootdir)

		self.WebAppRunner = aiohttp.web.AppRunner(
			self.WebApp,
			handle_signals=False,
			access_log=logging.getLogger(__name__[:__name__.rfind('.')] + '.al'),
			access_log_class=AccessLogger,
		)

		websvc._register_container(self, config_section_name)


	async def initialize(self, app):

		await self.WebAppRunner.setup()

		for addr, port in self._listen:
			site = aiohttp.web.TCPSite(self.WebAppRunner, addr, port)
			await site.start()


	async def finalize(self, app):
		await self.WebAppRunner.cleanup()


	async def _on_prepare_response(self, request, response):
		response.headers['Server'] = self.ServerTokens

		