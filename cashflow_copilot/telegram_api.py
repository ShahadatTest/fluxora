"""Small bounded Telegram transport. Never export token-bearing URLs/errors."""
import json
import re
import urllib.request


class TelegramError(Exception):pass


class TelegramAPI:
    def __init__(self,token,*,opener=None):
        if not isinstance(token,str) or not re.fullmatch(r'[0-9]{5,16}:[A-Za-z0-9_-]{20,160}',token):
            raise ValueError('Configure a valid Telegram bot token privately.')
        self.__token=token;self._opener=opener or urllib.request.urlopen

    def __repr__(self):return '<TelegramAPI token=[redacted]>'

    def call(self,method,payload):
        if method not in {'getMe','getUpdates','sendMessage','answerCallbackQuery','editMessageReplyMarkup'}:
            raise TelegramError('Unsupported Telegram operation.')
        data=json.dumps(payload,ensure_ascii=False).encode()
        if len(data)>32000:raise TelegramError('Telegram request too large.')
        request=urllib.request.Request('https://api.telegram.org/bot'+self.__token+'/'+method,data=data,
            headers={'Content-Type':'application/json'})
        try:
            with self._opener(request,timeout=15) as response:
                raw=response.read(262145)
                if len(raw)>262144:raise TelegramError('Telegram response too large.')
                result=json.loads(raw)
            if not isinstance(result,dict) or result.get('ok') is not True:raise TelegramError('Telegram request unavailable.')
            return result.get('result')
        except Exception:
            # Includes provider-controlled description and token in URL exceptions.
            raise TelegramError('Telegram request unavailable; no automatic send retry.') from None
