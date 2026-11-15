"""Offline Dispatcher tests using a fake Telegram session, no token/network needed."""
import asyncio
import itertools
from datetime import datetime,timezone
from aiogram import Bot,Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Update,Message,Chat,User
from aurelia_utils.bot import build_router,animated
from aurelia_utils.config import Config
from aurelia_utils.store import Store


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls=[]
        self.seq=itertools.count(100)
    async def close(self): pass
    async def stream_content(self,*args,**kwargs): yield b''
    async def make_request(self,bot,method,timeout=None):
        self.calls.append(method)
        if method.__api_method__ in {'answerCallbackQuery','sendMessageDraft'}: return True
        return Message(message_id=getattr(method,'message_id',None) or next(self.seq),
                       date=datetime.now(timezone.utc),chat=Chat(id=1,type='private'),
                       text=getattr(method,'text',None),reply_markup=getattr(method,'reply_markup',None)).as_(bot)


def test_seed_wizard_queue_and_stale_button(tmp_path):
    async def scenario():
        store=Store(tmp_path)
        fake=FakeSession()
        bot=Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789',session=fake)
        dp=Dispatcher()
        dp.include_router(build_router(Config(tmp_path),store))
        seq=itertools.count(1)
        async def message(text):
            await dp.feed_update(bot,Update(update_id=next(seq),message=Message(
                message_id=next(seq),date=datetime.now(timezone.utc),chat=Chat(id=1,type='private'),
                from_user=User(id=1,is_bot=False,first_name='Test'),text=text)))
        async def click(action,rev=None):
            from aiogram.types import CallbackQuery
            data=f'{rev or store.session(1)["rev"]}|{action}'
            await dp.feed_update(bot,Update(update_id=next(seq),callback_query=CallbackQuery(
                id=str(next(seq)),from_user=User(id=1,is_bot=False,first_name='Test'),chat_instance='test',
                data=data,message=Message(message_id=100,date=datetime.now(timezone.utc),chat=Chat(id=1,type='private')))))
        await message('/start')
        old=store.session(1)['rev']
        await click('seed')
        await message('2057')
        assert store.session(1)['params']['seed']=='2057'
        await click('run')
        assert len(store.jobs(1))==1 and store.jobs(1)[0]['status']=='queued'
        await click('seed',old)
        assert len(store.jobs(1))==1
        assert any('старого меню' in (getattr(c,'text','') or '') for c in fake.calls)
    asyncio.run(scenario())


def test_animation_disabled_does_not_stream():
    async def scenario():
        fake=FakeSession()
        bot=Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789',session=fake)
        await animated(bot,1,'текст '*1000,enabled=False)
        assert all(c.__api_method__=='sendMessage' for c in fake.calls)
        assert all(len(c.text.encode('utf-16-le'))//2<=4096 for c in fake.calls)
    asyncio.run(scenario())


def test_sketch_wizard_and_cross_user_access(tmp_path):
    async def scenario():
        from aiogram.types import CallbackQuery
        from PIL import Image,ImageDraw
        from aurelia_utils.worker import run_job
        cfg=Config(tmp_path)
        store=Store(tmp_path)
        source=tmp_path/'input.png'
        im=Image.new('L',(256,128),255)
        ImageDraw.Draw(im).rectangle((40,30,160,100),fill=0)
        im.save(source)
        parent=store.enqueue(1,'inspect',{},source=source)
        run_job(cfg,store,store.claim())
        fake=FakeSession()
        bot=Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789',session=fake)
        dp=Dispatcher()
        dp.include_router(build_router(cfg,store))
        seq=itertools.count(1)
        async def click(data,user=1):
            await dp.feed_update(bot,Update(update_id=next(seq),callback_query=CallbackQuery(
                id=str(next(seq)),from_user=User(id=user,is_bot=False,first_name='Test'),chat_instance='test',
                data=data,message=Message(message_id=100,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private')))))
        await click(f'source:{parent}',user=2)
        assert 'не найдено' in fake.calls[-1].text
        await click(f'source:{parent}')
        await click(store.session(1)['rev']+'|choose:dark')
        assert store.session(1)['mode']=='strict'
        await click(store.session(1)['rev']+'|run')
        child=store.claim()
        run_job(cfg,store,child)
        await click(f'png:{child["id"]}')
        assert fake.calls[-1].__api_method__=='sendDocument'
        await click(f'delete:{parent}')
        await click(f'confirmdelete:{parent}')
        assert store.get(parent,1) is not None
        assert 'Сначала удали' in fake.calls[-1].text
    asyncio.run(scenario())
