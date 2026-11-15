"""All user interaction lives in Telegram. Long polling, no HTTP server."""
import asyncio
import contextlib
import json
import logging
import secrets
import shutil
import uuid
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, FSInputFile, BotCommand, ErrorEvent)
from .config import Config
from .engine import Params
from .store import Store, Busy
from .worker import check_quota, job_dir

log = logging.getLogger(__name__)
HELP = (
    'Aurelia Utils делает контурные карты: белая суша и синяя вода\n\n'
    'По seed\nВведи слово или число. Одинаковые seed, настройки и версия движка дают одинаковую карту. '
    'Материковые ядра задают крупные массивы суши. Они могут соединяться или распадаться на острова\n\n'
    'По пятну\nПришли заливку суши на контрастном фоне. Лучше PNG документом: Telegram не сожмет картинку. '
    'Тонкие линии не превращаются в залитый материк сами по себе. Надписи и рамки тоже могут попасть в сушу\n\n'
    'Выбери светлое или темное и проверь маску. Прозрачный фон считается водой. '
    'Порог можно изменить от 1 до 254\n\n'
    'Точно по пятну сохраняет каждый пиксель маски в исходном размере. '
    'Увеличение идет через Nearest Neighbor. Уменьшение запрещено: острова мелкие, им и так непросто\n\n'
    'Живой берег меняет очертания в узкой полосе у воды. Мелкие острова могут измениться. '
    'Этот режим включается только по твоему выбору. Оригинал всегда остается в архиве\n\n'
    'PNG - полный размер. Картинка в чате - уменьшенный предпросмотр. '
    'Архив содержит PNG, параметры и исходник для режима пятна\n\n'
    'Сохраненные карты доступны через Мои карты. /cancel отменяет текущую задачу. '
    'В этой версии нет политических границ, подписей, рельефа и расчета площадей'
)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=action) for text, action in row]
        for row in rows])


async def api(call):
    for attempt in range(3):
        try:
            return await call()
        except TelegramRetryAfter as e:
            if attempt == 2:
                raise
            await asyncio.sleep(e.retry_after + .1)


async def edit(message: Message, text, markup=None):
    try:
        return await api(lambda: message.edit_text(text, reply_markup=markup))
    except TelegramBadRequest as e:
        if 'message is not modified' in str(e):
            return message
        # A deleted/old panel can be replaced without breaking the workflow.
        return await api(lambda: message.answer(text, reply_markup=markup))


async def animated(bot, chat, text, enabled=True):
    # 1800 Unicode code points fit into 4096 UTF-16 units even with emoji.
    for offset in range(0, len(text), 1800):
        part = text[offset:offset+1800]
        if not enabled or len(part) < 450:
            await api(lambda: bot.send_message(chat, part))
            continue
        draft = secrets.randbelow(2**30)+1
        fallback = None
        use_draft = chat > 0 and hasattr(bot, 'send_message_draft')
        for end in range(500, len(part), 500):
            if use_draft:
                try:
                    await api(lambda: bot.send_message_draft(chat_id=chat, draft_id=draft, text=part[:end]))
                except TelegramBadRequest:
                    use_draft = False
            if not use_draft:
                if fallback is None:
                    fallback = await api(lambda: bot.send_message(chat, part[:end]))
                else:
                    fallback = await edit(fallback, part[:end])
            await asyncio.sleep(1.3)
        if fallback:
            await edit(fallback, part)
        else:
            # Drafts are temporary: persist the final response.
            await api(lambda: bot.send_message(chat, part))


class PrivateSerial(BaseMiddleware):
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)

    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        message = event.message if isinstance(event, CallbackQuery) else event
        if not user or not isinstance(message, Message):
            return
        if message.chat.type != 'private':
            if isinstance(event, CallbackQuery):
                await event.answer('Открой личный чат с ботом', show_alert=True)
            elif message.text and message.text.startswith('/start'):
                await message.answer('Открой личный чат с ботом - карты хранятся у каждого отдельно')
            return
        async with self.locks[user.id]:
            return await handler(event, data)


def build_router(config: Config, store: Store):
    router = Router()
    middleware = PrivateSerial()
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)

    async def panel(message, user, text, rows, session):
        revision = uuid.uuid4().hex[:8]
        session['rev'] = revision
        rows = [[(t, f'{revision}|{a}') for t,a in row] for row in rows]
        result = await edit(message, text, keyboard(rows))
        session['panel'] = result.message_id
        store.set_session(user, session)
        return result

    async def home(message, user):
        await panel(message, user,
                    'Aurelia Utils\n\nБелая суша. Синяя вода\nС чего начнем?',
                    [[('🌍 По seed','seed'), ('🖌 По пятну','spot')],
                     [('🗂 Мои карты','list:0')], [('⚙️ Настройки','settings'), ('Как это работает','help')]], {})

    async def wizard(message, user, session):
        p = session['params']
        sketch = session['kind'] == 'sketch'
        size = f"{p['width']:,} x {p['height']:,}".replace(',', ' ')
        lines = ['Параметры карты', '', f'Seed: {p["seed"]}', f'Размер: {size} px']
        if sketch:
            lines += [f'Суша: {"темное" if session["polarity"]=="dark" else "светлое"}',
                      f'Порог: {session.get("threshold",128)}',
                      f'Берег: {"точно по пятну" if session["mode"]=="strict" else "живой"}']
        else:
            lines += [f'Суша: {p["land"]} %', f'Материковые ядра: {p["continents"]}', f'Изрезанность: {p["roughness"]} / 100']
        rows = [[('🗺 Сделать карту','run')], [('Seed','value:seed'), ('Размер','sizes')]]
        if sketch:
            rows += [[('Точно по пятну','mode:strict'), ('Живой берег','mode:coast')],
                     [('Поменять цвет суши','polarity'), ('Порог','value:threshold')],
                     [('Проверить маску','mask')]]
            if session['mode'] == 'coast':
                lines += ['', 'Живой берег изменит очертания. Мелкие острова могут измениться']
        else:
            rows += [[('Доля суши','value:land'), ('Материковые ядра','value:continents')],
                     [('Изрезанность берега','value:roughness')]]
        rows += [[('← В меню','home')]]
        session.pop('await', None)
        await panel(message, user, '\n'.join(lines), rows, session)

    async def listing(message, user, offset=0):
        jobs = store.jobs(user, 8, offset)
        rows = []
        for j in jobs:
            title = {'seed':'Карта по seed','sketch':'Карта по пятну','inspect':'Исходник пятна'}[j['kind']]
            status = {'queued':'в очереди','running':'обработка','cancelling':'отмена','ready':'готово','cancelled':'отменено','failed':'ошибка'}[j['status']]
            rows.append([(f'{title} · {status} · {j["id"][:4]}', f'open:{j["id"]}')])
        if offset:
            rows.append([('← Назад', f'list:{max(0,offset-8)}')])
        if len(jobs) == 8:
            rows.append([('Дальше →',f'list:{offset+8}')])
        rows.append([('← В меню','home')])
        await panel(message,user,'Мои карты\n\n'+('Открой результат или продолжи работу с пятном' if jobs else 'Пока пусто'), rows, {})

    async def queue(message, user, session):
        check_quota(config, user, 12_000_000)
        payload = {k:v for k,v in session.items() if k in {'params','source_job','polarity','mode','threshold'}}
        # Queue gets its own status message so editing a menu never races progress updates.
        status = await message.answer('В очереди')
        try:
            jid = store.enqueue(user, session['kind'], payload, message.chat.id, status.message_id)
        except ValueError:
            await status.delete()
            raise
        await edit(status, 'В очереди\nМожно закрыть чат - результат сохранится',
                   keyboard([[('Отменить',f'cancel:{jid}')]]))
        await home(message, user)

    async def open_job(message, user, jid):
        job = store.get(jid, user)
        if not job:
            await message.answer('Карта не найдена')
            return
        folder = job_dir(config, user, jid)
        if job['status'] == 'ready' and job['kind'] == 'inspect':
            meta = json.loads((folder/'metadata.json').read_text())
            w,h = meta['source_size']
            resume=json.loads(job['params']).get('resume',{})
            session = {'kind':'sketch','source_job':jid,'mode':'strict','polarity':'dark',
                       'threshold':json.loads(job['params']).get('threshold',128),
                       'params':asdict(Params(seed=str(secrets.randbelow(2**32)),width=w,height=h))}
            if resume:
                session.update({k:v for k,v in resume.items() if k in {'params','mode','polarity','threshold'}})
            rows = []
            for polarity, label in [('dark','Темное - суша'),('light','Светлое - суша')]:
                if meta.get(polarity):
                    await message.answer_photo(FSInputFile(folder/f'{polarity}.png'),caption=label)
                    rows.append([(label,f'choose:{polarity}')])
            rows += [[('← В меню','home')]]
            await panel(message,user,'Выбери что считать сушей\nИсходник сохранен. Надписи и рамки тоже попадают в маску',rows,session)
        elif job['status'] == 'ready':
            await message.answer_photo(FSInputFile(folder/'preview.png'), caption='Уменьшенный предпросмотр',
                                       reply_markup=result_keys(jid))
        elif job['status'] in {'queued','running','cancelling'}:
            await message.answer(job['stage'],reply_markup=keyboard([[('Отменить',f'cancel:{jid}')]]))
        else:
            await message.answer(job['error'] or 'Задача отменена')
        # Deletion is explicit and confirmed; referenced source files are protected.
        await message.answer('Управление сохранением',reply_markup=keyboard([[('Удалить',f'delete:{jid}')]]))

    @router.message(CommandStart())
    async def start(message: Message):
        welcome = await message.answer('Aurelia Utils')
        await home(welcome, message.from_user.id)

    @router.message(Command('cancel'))
    async def cancel_command(message: Message):
        for job in store.jobs(message.from_user.id,50):
            if job['status'] in {'queued','running','cancelling'}:
                store.cancel(job['id'],message.from_user.id)
        welcome = await message.answer('Текущая задача отменена')
        await home(welcome,message.from_user.id)

    @router.callback_query()
    async def callback(query: CallbackQuery, bot: Bot):
        await query.answer()
        message, user, action = query.message, query.from_user.id, query.data or ''
        try:
            # Result buttons are durable across restarts. Ownership is checked every time.
            if '|' not in action:
                kind, _, value = action.partition(':')
                job = store.get(value,user)
                if not job:
                    await message.answer('Сохранение не найдено')
                    return
                if kind == 'cancel':
                    store.cancel(value,user)
                    await message.answer('Отменяю задачу')
                elif kind in {'png','zip'} and job['status']=='ready' and job['kind']!='inspect':
                    path = job_dir(config,user,value)/('map.png' if kind=='png' else 'project.zip')
                    if path.stat().st_size > 49_000_000:
                        await message.answer('Файл больше лимита отправки. Исходник сохранен. Выбери размер поменьше')
                    else:
                        await api(lambda: message.answer_document(FSInputFile(path,filename=f'Aurelia_{value}.{kind if kind=="zip" else "png"}')))
                elif kind == 'source':
                    fresh = await message.answer('Открываю пятно')
                    await open_job(fresh,user,value)
                elif kind == 'delete':
                    await message.answer('Удалить эту работу вместе с файлами?',reply_markup=keyboard(
                        [[('Удалить',f'confirmdelete:{value}'),('Оставить',f'keep:{value}')]]))
                elif kind == 'keep':
                    await edit(message,'Оставил сохранение')
                elif kind == 'confirmdelete':
                    # A source may be needed by queued jobs and saved maps.
                    with store.connect() as db:
                        linked = db.execute("SELECT params FROM jobs WHERE user=? AND kind='sketch'",(user,)).fetchall()
                    if any(json.loads(r[0]).get('source_job') == value for r in linked):
                        await message.answer('Это исходник сохраненной карты. Сначала удали карты которые его используют')
                    elif store.remove(value,user):
                        shutil.rmtree(job_dir(config,user,value),ignore_errors=True)
                        await edit(message,'Удалено')
                    else:
                        await message.answer('Сначала отмени обработку и дождись ее остановки')
                return
            revision, action = action.split('|',1)
            session = store.session(user)
            if session.get('rev') != revision:
                await message.answer('Эта кнопка от старого меню. Открой /start')
                return
            if action == 'home':
                await home(message,user)
            elif action == 'seed':
                session = {'kind':'seed','params':asdict(Params()),'await':'seed'}
                await panel(message,user,'Пришли seed - слово или число\nИли доверь это случаю',
                            [[('🎲 Случайный seed','random')],[('← В меню','home')]], session)
            elif action == 'random':
                session['params']['seed'] = str(secrets.randbelow(2**32))
                await wizard(message,user,session)
            elif action == 'spot':
                await panel(message,user,
                            'Пришли пятно суши на контрастном фоне\n\nЛучше PNG документом. '
                            'Белое на черном или черное на белом - выберешь после загрузки\n'
                            'До 20 МБ и 33 554 432 px. Прозрачный фон станет водой',
                            [[('← В меню','home')]], {'await':'upload'})
            elif action == 'settings':
                enabled = store.animation(user)
                await panel(message,user,'Настройки\n\nАнимация применяется к длинной справке. '
                            'Простые действия идут сразу',[[('Анимация: '+('вкл' if enabled else 'выкл'),'toggle')],
                            [('← В меню','home')]],{})
            elif action == 'toggle':
                store.animation(user,True)
                enabled = store.animation(user)
                await panel(message,user,'Настройки',[[('Анимация: '+('вкл' if enabled else 'выкл'),'toggle')],
                            [('← В меню','home')]],{})
            elif action == 'help':
                await animated(bot,message.chat.id,HELP,store.animation(user))
            elif action.startswith('list:'):
                await listing(message,user,int(action.split(':')[1]))
            elif action.startswith('open:'):
                await open_job(message,user,action.split(':')[1])
            elif action.startswith('choose:'):
                session['polarity'] = action.split(':')[1]
                await wizard(message,user,session)
            elif action.startswith('value:'):
                field = action.split(':')[1]
                prompts = {'seed':'Пришли seed от 1 до 128 символов', 'land':'Сколько суши? От 5 до 90 %',
                           'continents':'Сколько материковых ядер? От 1 до 12\nОни могут соединяться или распадаться на острова',
                           'roughness':'Изрезанность берега от 0 до 100',
                           'threshold':'Порог яркости от 1 до 254\nПосле изменения проверь маску',
                           'size':'Размер в пикселях, например 6 144 x 3 072\nМаксимум 134 217 728 px. Сторона до 16 384 px'}
                if field not in prompts:
                    return
                session['await'] = field
                await panel(message,user,prompts[field],[[('← К параметрам','back')]],session)
            elif action == 'back':
                await wizard(message,user,session)
            elif action == 'sizes':
                rows=[]
                for w in (2048,4096,8192,16384):
                    h = w//2
                    if session['kind']=='sketch':
                        source = store.get(session['source_job'],user)
                        sw,sh = json.loads((job_dir(config,user,source['id'])/'metadata.json').read_text())['source_size']
                        h=round(sh*w/sw)
                        if w<sw or h<sh or w*h>134217728 or h>16384:
                            continue
                    rows.append([(f'{w:,} x {h:,}'.replace(',',' '),f'size:{w}:{h}')])
                rows += [[('Свой размер','value:size')],[('← К параметрам','back')]]
                await panel(message,user,'Выбери размер\nПятно можно только увеличивать с сохранением пропорций',rows,session)
            elif action.startswith('size:'):
                _,w,h=action.split(':')
                session['params'].update(width=int(w),height=int(h))
                await wizard(message,user,session)
            elif action.startswith('mode:'):
                session['mode'] = action.split(':')[1]
                await wizard(message,user,session)
            elif action == 'polarity':
                session['polarity']='light' if session['polarity']=='dark' else 'dark'
                await wizard(message,user,session)
            elif action == 'mask':
                # Preview work is queued too; no image decoding in the bot process.
                src = job_dir(config,user,session['source_job'])/'source'
                status = await message.answer('Проверяю маску')
                check_quota(config,user,src.stat().st_size+12_000_000)
                jid=store.enqueue(user,'inspect',{'threshold':session.get('threshold',128),'resume':session},message.chat.id,status.message_id,source=src)
                await edit(status,'Проверяю маску',keyboard([[('Отменить',f'cancel:{jid}')]]))
            elif action == 'run':
                Params(**session['params']).validate()
                await queue(message,user,session)
        except (ValueError, OSError) as e:
            await message.answer(str(e) if isinstance(e,ValueError) else 'Файл недоступен. Открой сохранение заново')

    @router.message(F.document | F.photo)
    async def upload(message: Message, bot: Bot):
        user=message.from_user.id
        session=store.session(user)
        if session.get('await') != 'upload':
            await message.answer('Сначала выбери По пятну в меню /start')
            return
        doc=message.document or message.photo[-1]
        if doc.file_size and doc.file_size>config.upload_bytes:
            await message.answer('Файл больше 20 МБ. Пришли PNG поменьше')
            return
        incoming=config.user_dir(user)/f'upload_{uuid.uuid4().hex}.part'
        try:
            check_quota(config,user,config.upload_bytes+12_000_000)
            with store.connect() as db:
                active=db.execute("SELECT 1 FROM jobs WHERE user=? AND status IN ('queued','running','cancelling')",(user,)).fetchone()
            if active:
                raise Busy('У тебя уже есть задача. Дождись результата или отмени ее')
            file=await bot.get_file(doc.file_id)
            if file.file_size and file.file_size>config.upload_bytes:
                raise ValueError('Файл больше 20 МБ')
            class Bounded:
                def __init__(self, file): self.file, self.count = file, 0
                def write(self, chunk):
                    self.count += len(chunk)
                    if self.count>config.upload_bytes:
                        raise ValueError('Файл больше 20 МБ')
                    return self.file.write(chunk)
                def flush(self): self.file.flush()
                def seek(self,*args): return self.file.seek(*args)
            with incoming.open('wb') as stream:
                await bot.download_file(file.file_path,Bounded(stream),timeout=60)
            status=await message.answer('Читаю пятно'+('\nФото сжато Telegram. Для точных пикселей отправляй документ' if message.photo else ''))
            # source readiness is transactional via staging/activation in Store.
            jid=store.enqueue(user,'inspect',{},message.chat.id,status.message_id,source=incoming)
            await edit(status,'Читаю пятно',keyboard([[('Отменить',f'cancel:{jid}')]]))
            store.set_session(user,{})
        except (ValueError,TelegramBadRequest,asyncio.TimeoutError) as e:
            await message.answer(str(e) if isinstance(e,ValueError) else 'Не удалось загрузить файл. Проверь размер и попробуй еще раз')
        finally:
            incoming.unlink(missing_ok=True)

    @router.message(F.text)
    async def text_input(message: Message):
        user=message.from_user.id
        session=store.session(user)
        field=session.get('await')
        if field not in {'seed','land','continents','roughness','threshold','size'}:
            await message.answer('Открой /start и выбери действие')
            return
        try:
            text=(message.text or '').strip()
            if field=='seed':
                session['params']['seed']=text
            elif field=='size':
                text=text.lower().replace('×','x').replace('х','x').replace(' ','')
                w,h=map(int,text.split('x'))
                session['params'].update(width=w,height=h)
                if session['kind']=='sketch':
                    meta=json.loads((job_dir(config,user,session['source_job'])/'metadata.json').read_text())
                    sw,sh=meta['source_size']
                    if w<sw or h<sh or abs(w/h-sw/sh)>.002:
                        raise ValueError('Нельзя уменьшать пятно или менять пропорции')
            elif field=='threshold':
                value=int(text)
                if not 1<=value<=254: raise ValueError('Порог от 1 до 254')
                session['threshold']=value
            else:
                session['params'][field]=int(text.replace('%',''))
            Params(**session['params']).validate()
            # Use the existing bot panel after receiving a value, keep the chat compact.
            panel_message=Message(message_id=session.get('panel',message.message_id),date=message.date,
                                  chat=message.chat).as_(message.bot)
            await wizard(panel_message,user,session)
        except (ValueError,TypeError,ZeroDivisionError):
            await message.answer('Значение не подошло. Проверь диапазон и пришли еще раз')

    @router.errors()
    async def errors(event: ErrorEvent):
        log.error('Telegram handler failed: %s',type(event.exception).__name__)
        update=event.update
        message=update.message or (update.callback_query.message if update.callback_query else None)
        if isinstance(message,Message):
            with contextlib.suppress(Exception):
                await message.answer('Действие прервалось. Сохраненные карты на месте. Открой /start')
        return True

    return router


def result_keys(jid):
    return keyboard([[('📥 PNG',f'png:{jid}'),('📦 Архив',f'zip:{jid}')],
                     [('Удалить',f'delete:{jid}')]])


async def notify(bot,config,store):
    last={}
    while True:
        for job in store.pending_delivery():
            jid=job['id']
            try:
                if job['status'] in {'queued','running','cancelling'}:
                    stamp=(job['stage'],job['done'],job['total'])
                    if last.get(jid)!=stamp:
                        text=job['stage']
                        if job['total']>1:
                            text+=f'\nСтрок: {job["done"]:,} / {job["total"]:,}'.replace(',',' ')
                        try:
                            await api(lambda:bot.edit_message_text(chat_id=job['chat'],message_id=job['message'],text=text,
                                      reply_markup=keyboard([[('Отменить',f'cancel:{jid}')]])))
                        except TelegramBadRequest:
                            pass
                        last[jid]=stamp
                    continue
                if job['status']=='ready':
                    folder=job_dir(config,job['user'],jid)
                    if job['kind']=='inspect':
                        await api(lambda:bot.send_message(job['chat'],'Пятно прочитано. Выбери что считать сушей',
                                  reply_markup=keyboard([[('Открыть пятно',f'source:{jid}')], [('Удалить',f'delete:{jid}')]])))
                    else:
                        meta=json.loads((folder/'metadata.json').read_text())
                        p=meta['params']
                        await api(lambda:bot.send_photo(job['chat'],FSInputFile(folder/'preview.png'),
                            caption=f'Готово\n{p["width"]:,} x {p["height"]:,} px\nSeed: {p["seed"]}\nПолный размер - кнопкой PNG'.replace(',',' '),
                            reply_markup=result_keys(jid)))
                    summary='Готово. Результат сохранен'
                elif job['status']=='failed':
                    summary=job['error'] or 'Обработка прервалась'
                    await api(lambda:bot.send_message(job['chat'],summary,
                              reply_markup=keyboard([[('Удалить работу',f'delete:{jid}')]])))
                else:
                    summary='Задача отменена'
                with contextlib.suppress(TelegramBadRequest):
                    await api(lambda:bot.edit_message_text(chat_id=job['chat'],message_id=job['message'],text=summary))
                store.delivered(jid)
                last.pop(jid,None)
            except TelegramForbiddenError:
                store.delivered(jid)
            except Exception as e:
                log.warning('Notification postponed: %s',type(e).__name__)
        await asyncio.sleep(2.5)


async def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    config=Config.env()
    if not config.token:
        raise SystemExit('Задай BOT_TOKEN в .env')
    store=Store(config.root)
    bot=Bot(config.token)
    dispatcher=Dispatcher()
    dispatcher.include_router(build_router(config,store))
    await bot.set_my_commands([BotCommand(command='start',description='Открыть Aurelia Utils'),
                               BotCommand(command='cancel',description='Отменить текущую задачу')])
    task=asyncio.create_task(notify(bot,config,store))
    try:
        await dispatcher.start_polling(bot,tasks_concurrency_limit=20)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError): await task
        await bot.session.close()


if __name__=='__main__':
    asyncio.run(main())
