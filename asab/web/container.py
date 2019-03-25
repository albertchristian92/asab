import re
import asyncio
import logging
import aiohttp

from ..config import ConfigObject
from ..net import SSLContextBuilder
from .accesslog import AccessLogger


class WebContainer(ConfigObject):

	'''
# Configuration examples

## Simple HTTP on 8080

[web]
listen=0.0.0.0 8080

## Multiple interfaces

[web]
listen:
	0.0.0.0 8080
	:: 8080


## Multiple interfaces, one with HTTPS

[web]
listen:
	0.0.0.0 8080
	:: 8080
	0.0.0.0 8443 ssl:web
	'''


	ConfigDefaults = {
		'listen': '0.0.0.0 8080', # Can be multiline
		'backlog': 128,
		'rootdir': '',
		'servertokens': 'full', # Controls whether 'Server' response header field is included ('full') or faked 'prod' ()
	}


	def __init__(self, websvc, config_section_name, config=None):
		super().__init__(config_section_name=config_section_name, config=config)

		self.BackLog = int(self.Config.get("backlog"))

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
		for line in ls.split('\n'):
			line = line.strip()
			if len(line) == 0: continue
			line = re.split(r"\s+", line)
			
			addr = line.pop(0)
			port = line.pop(0)
			port = int(port)
			ssl = None

			for param in line:
				if param.startswith('ssl:'):
					ssl = SSLContextBuilder(param).build()
				else:
					raise RuntimeError("Unknown asab:web listen parameter: '{}'".format(param))
			self._listen.append((addr, port, ssl))

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

		for addr, port, ssl_context in self._listen:
			site = aiohttp.web.TCPSite(self.WebAppRunner,
				host=addr, port=port, backlog=self.BackLog,
				ssl_context = ssl_context,
			)
			await site.start()


	async def finalize(self, app):
		await self.WebAppRunner.cleanup()


	async def _on_prepare_response(self, request, response):
		response.headers['Server'] = self.ServerTokens

		