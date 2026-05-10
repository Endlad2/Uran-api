import asyncio
import json
import sqlite3
import os
import zipfile
import random
import string
import hashlib
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from telethon import TelegramClient, errors
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import User
import threading
import time
from io import BytesIO
import configparser

config = configparser.ConfigParser()
config_path = 'tg_config.ini'

if not os.path.exists(config_path):
    print(f"Файл конфигурации {config_path} не найден")
    print("Используйте POST /configure?app_id=YOUR_API_ID&app_hash=YOUR_API_HASH для настройки")
    API_ID = None
    API_HASH = None
else:
    config.read(config_path)
    API_ID = config.getint('Telegram', 'api_id') if config.has_section('Telegram') and config.has_option('Telegram', 'api_id') else None
    API_HASH = config.get('Telegram', 'api_hash') if config.has_section('Telegram') and config.has_option('Telegram', 'api_hash') else None

PORT = 9870
BASE_URL = f'http://localhost:{PORT}'
CACHE_ARCHIVE = 'cache.zip'
DB_NAME = 'telegram_cache.db'
CLEANUP_INTERVAL_HOURS = 1
MAX_VIDEO_SIZE_MB = 20
SESSION_NAME = 'user_session'

app = Flask(__name__)
event_loop = None

class TelegramCacheManager:
    def __init__(self):
        self.db_path = DB_NAME
        self.cache_archive = CACHE_ARCHIVE
        self.client = None
        self.session_name = SESSION_NAME
        self.api_id = API_ID
        self.api_hash = API_HASH
        self.base_url = BASE_URL
        self.init_database()
        self.init_archives()
        self.start_cleanup_scheduler()
    
    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS session (
                id INTEGER PRIMARY KEY,
                session_string TEXT,
                user_data TEXT,
                created_at TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chats_cache (
                id INTEGER PRIMARY KEY,
                chat_id TEXT,
                chat_data TEXT,
                last_update TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                messages TEXT,
                last_update TIMESTAMP,
                UNIQUE(chat_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                asset_type TEXT,
                file_name TEXT,
                chat_id TEXT,
                message_id INTEGER,
                file_size INTEGER,
                created_at TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS large_videos (
                asset_id TEXT PRIMARY KEY,
                chat_id TEXT,
                message_id INTEGER,
                file_size INTEGER,
                access_hash TEXT,
                created_at TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def init_archives(self):
        if not os.path.exists(self.cache_archive):
            with zipfile.ZipFile(self.cache_archive, 'w', zipfile.ZIP_DEFLATED) as zipf:
                pass
    
    def generate_asset_id(self, chat_id, message_id, asset_type):
        unique_str = f"{chat_id}_{message_id}_{asset_type}"
        return hashlib.md5(unique_str.encode()).hexdigest()[:20]
    
    def generate_random_filename(self, extension):
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for _ in range(20)) + extension
    
    def get_asset_url(self, asset_type, asset_id):
        if asset_type == 'photo':
            return f"{self.base_url}/get_asset/photo?asset_id={asset_id}"
        elif asset_type == 'video' or asset_type == 'video_note':
            return f"{self.base_url}/get_asset/video?asset_id={asset_id}"
        elif asset_type == 'sticker':
            return f"{self.base_url}/get_asset/sticker?asset_id={asset_id}"
        elif asset_type == 'document':
            return f"{self.base_url}/get_asset/document?asset_id={asset_id}"
        return None
    
    async def download_and_cache_asset(self, message, chat_id, message_id, asset_type):
        asset_id = self.generate_asset_id(chat_id, message_id, asset_type)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT file_name FROM assets WHERE asset_id = ?", (asset_id,))
        existing = cursor.fetchone()
        
        if existing:
            conn.close()
            return asset_id
        
        try:
            file_path = None
            file_size = 0
            
            if asset_type == 'photo':
                temp_data = BytesIO()
                await self.client.download_media(message.photo, temp_data)
                temp_data.seek(0)
                
                extension = '.jpg'
                filename = self.generate_random_filename(extension)
                
                with zipfile.ZipFile(self.cache_archive, 'a', zipfile.ZIP_DEFLATED) as zipf:
                    zipf.writestr(filename, temp_data.getvalue())
                
                file_path = filename
                file_size = len(temp_data.getvalue())
                    
            elif asset_type in ['video', 'video_note']:
                media = None
                if hasattr(message, 'video') and message.video:
                    media = message.video
                elif hasattr(message, 'document') and message.document:
                    media = message.document
                
                if media:
                    file_size = media.size if hasattr(media, 'size') else 0
                    
                    if file_size < MAX_VIDEO_SIZE_MB * 1024 * 1024:
                        temp_data = BytesIO()
                        await self.client.download_media(message, temp_data)
                        temp_data.seek(0)
                        
                        extension = '.mp4'
                        filename = self.generate_random_filename(extension)
                        
                        with zipfile.ZipFile(self.cache_archive, 'a', zipfile.ZIP_DEFLATED) as zipf:
                            zipf.writestr(filename, temp_data.getvalue())
                        
                        file_path = filename
                    else:
                        access_hash = str(media.access_hash) if hasattr(media, 'access_hash') else None
                        cursor.execute('''
                            INSERT OR REPLACE INTO large_videos 
                            (asset_id, chat_id, message_id, file_size, access_hash, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (asset_id, chat_id, message_id, file_size, access_hash, datetime.now()))
                        conn.commit()
                        conn.close()
                        return asset_id
                    
            elif asset_type == 'sticker':
                if message.sticker:
                    temp_data = BytesIO()
                    await self.client.download_media(message.sticker, temp_data)
                    temp_data.seek(0)
                    
                    extension = '.webp'
                    filename = self.generate_random_filename(extension)
                    
                    with zipfile.ZipFile(self.cache_archive, 'a', zipfile.ZIP_DEFLATED) as zipf:
                        zipf.writestr(filename, temp_data.getvalue())
                    
                    file_path = filename
                    file_size = len(temp_data.getvalue())
            
            if file_path:
                cursor.execute('''
                    INSERT OR REPLACE INTO assets 
                    (asset_id, asset_type, file_name, chat_id, message_id, file_size, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (asset_id, asset_type, file_path, chat_id, message_id, file_size, datetime.now()))
                conn.commit()
            
        except Exception as e:
            print(f"Ошибка скачивания ассета: {e}")
        finally:
            conn.close()
        
        return asset_id
    
    async def get_asset_file(self, asset_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT asset_type, file_name FROM assets WHERE asset_id = ?", (asset_id,))
        asset = cursor.fetchone()
        
        if asset:
            asset_type, file_name = asset
            
            if zipfile.is_zipfile(self.cache_archive):
                with zipfile.ZipFile(self.cache_archive, 'r') as zipf:
                    if file_name in zipf.namelist():
                        data = zipf.read(file_name)
                        conn.close()
                        return data, asset_type
        
        cursor.execute("SELECT chat_id, message_id FROM large_videos WHERE asset_id = ?", (asset_id,))
        large_video = cursor.fetchone()
        
        if large_video:
            conn.close()
            return None, 'video_large'
        
        conn.close()
        return None, None
    
    async def download_large_video(self, asset_id, chat_id, message_id):
        try:
            entity = await self.client.get_entity(int(chat_id))
            message = await self.client.get_messages(entity, ids=int(message_id))
            
            if message and hasattr(message, 'video') and message.video:
                temp_data = BytesIO()
                await self.client.download_media(message.video, temp_data)
                temp_data.seek(0)
                return temp_data.getvalue()
        except Exception as e:
            print(f"Ошибка скачивания большого видео: {e}")
        
        return None
    
    def cleanup_cache(self):
        print("Запуск очистки кэша...")
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM assets")
        cursor.execute("DELETE FROM large_videos")
        cursor.execute("DELETE FROM messages_cache")
        cursor.execute("DELETE FROM chats_cache")
        
        if os.path.exists(self.cache_archive):
            os.remove(self.cache_archive)
            with zipfile.ZipFile(self.cache_archive, 'w', zipfile.ZIP_DEFLATED) as zipf:
                pass
        
        conn.commit()
        conn.close()
        
        print("Очистка кэша завершена")
    
    def start_cleanup_scheduler(self):
        def cleanup_job():
            while True:
                time.sleep(CLEANUP_INTERVAL_HOURS * 3600)
                self.cleanup_cache()
        
        cleanup_thread = threading.Thread(target=cleanup_job, daemon=True)
        cleanup_thread.start()
    
    async def get_client(self):
        if not self.client or not self.client.is_connected():
            if not self.api_id or not self.api_hash:
                raise Exception("API_ID и API_HASH не настроены. Используйте POST /configure")
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
            await self.client.connect()
        return self.client
    
    async def cache_chats(self, chats_data):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO chats_cache (id, chat_data, last_update)
            VALUES (1, ?, ?)
        ''', (json.dumps(chats_data), datetime.now()))
        conn.commit()
        conn.close()
    
    async def get_cached_chats(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_data FROM chats_cache WHERE id = 1")
        result = cursor.fetchone()
        conn.close()
        return json.loads(result[0]) if result else None
    
    async def cache_messages(self, chat_id, messages_data):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO messages_cache (chat_id, messages, last_update)
            VALUES (?, ?, ?)
        ''', (str(chat_id), json.dumps(messages_data), datetime.now()))
        conn.commit()
        conn.close()
    
    async def get_cached_messages(self, chat_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT messages FROM messages_cache WHERE chat_id = ?", (str(chat_id),))
        result = cursor.fetchone()
        conn.close()
        return json.loads(result[0]) if result else None


cache_manager = TelegramCacheManager()


def run_async(coro):
    global event_loop
    
    if event_loop is None or event_loop.is_closed():
        event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(event_loop)
    
    return event_loop.run_until_complete(coro)


@app.route('/configure', methods=['POST'])
def configure():
    app_id = request.args.get('app_id')
    app_hash = request.args.get('app_hash')
    
    if not app_id or not app_hash:
        return jsonify({'error': 'app_id and app_hash are required'}), 400
    
    try:
        config = configparser.ConfigParser()
        config['Telegram'] = {
            'api_id': app_id,
            'api_hash': app_hash
        }
        
        with open(config_path, 'w') as f:
            config.write(f)
        
        global API_ID, API_HASH, cache_manager
        API_ID = int(app_id)
        API_HASH = app_hash
        cache_manager.api_id = API_ID
        cache_manager.api_hash = API_HASH
        
        return jsonify({'status': 'success', 'message': 'Configuration saved'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test/configure', methods=['GET'])
def test_configure():
    config = configparser.ConfigParser()
    config_path = 'tg_config.ini'
    
    if not os.path.exists(config_path):
        return jsonify({'status': False, 'message': 'Config file not found'})
    
    config.read(config_path)
    
    if not config.has_section('Telegram'):
        return jsonify({'status': False, 'message': 'Telegram section not found'})
    
    api_id = config.get('Telegram', 'api_id') if config.has_option('Telegram', 'api_id') else None
    api_hash = config.get('Telegram', 'api_hash') if config.has_option('Telegram', 'api_hash') else None
    
    if api_id and api_hash and api_id != 'YOUR_API_ID' and api_hash != 'YOUR_API_HASH':
        return jsonify({'status': True, 'message': 'Configuration is valid'})
    else:
        return jsonify({'status': False, 'message': 'Configuration not filled or contains placeholder values'})


@app.route('/test/auth', methods=['GET'])
def test_auth():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT session_string FROM session WHERE id = 1")
    result = cursor.fetchone()
    conn.close()
    
    if result and result[0]:
        return jsonify({'status': True, 'message': 'Active session found'})
    else:
        return jsonify({'status': False, 'message': 'No active session. Please login first'})


@app.route('/login/tel', methods=['GET'])
def login_tel():
    phone = request.args.get('number')
    if not phone:
        return jsonify({'error': 'Phone number required'}), 400
    
    async def _login():
        client = await cache_manager.get_client()
        try:
            await client.send_code_request(phone)
            return jsonify({'status': 'code_sent', 'phone': phone})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return run_async(_login())


@app.route('/login/tel/code', methods=['GET'])
def login_code():
    code = request.args.get('code')
    phone = request.args.get('phone')
    
    if not code:
        return jsonify({'error': 'Code required'}), 400
    
    async def _verify():
        client = await cache_manager.get_client()
        try:
            if phone:
                await client.sign_in(phone, code)
            else:
                await client.sign_in(code=code)
            
            session_string = client.session.save()
            me = await client.get_me()
            user_data = {
                'id': me.id,
                'first_name': me.first_name,
                'username': me.username,
                'phone': me.phone
            }
            
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO session (id, session_string, user_data, created_at)
                VALUES (1, ?, ?, ?)
            ''', (session_string, json.dumps(user_data), datetime.now()))
            conn.commit()
            conn.close()
            
            return jsonify({'status': 'success', 'user': user_data})
        except SessionPasswordNeededError:
            return jsonify({'status': 'password_needed'}), 403
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return run_async(_verify())


@app.route('/chat_list', methods=['GET'])
def chat_list():
    async def _get_chats():
        try:
            client = await cache_manager.get_client()
            
            try:
                chats = []
                async for dialog in client.iter_dialogs(limit=100):
                    if isinstance(dialog.entity, User):
                        name = dialog.entity.first_name or dialog.entity.username or str(dialog.entity.id)
                        chat_type = "user"
                    else:
                        name = dialog.entity.title if hasattr(dialog.entity, 'title') else dialog.name
                        chat_type = "channel"
                    
                    chats.append({
                        'id': dialog.id,
                        'name': name,
                        'type': chat_type,
                        'unread': dialog.unread_count
                    })
                
                await cache_manager.cache_chats(chats)
                return jsonify({'chats': chats})
            except Exception as e:
                cached = await cache_manager.get_cached_chats()
                if cached:
                    return jsonify({'chats': cached, 'cached': True})
                raise e
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return run_async(_get_chats())


@app.route('/get_messages/username', methods=['GET'])
def get_messages_by_username():
    username = request.args.get('username')
    limit = int(request.args.get('limit', 50))
    
    if not username:
        return jsonify({'error': 'Username required'}), 400
    
    async def _get_messages():
        try:
            client = await cache_manager.get_client()
            entity = await client.get_entity(username)
            return await _process_messages(client, entity, limit)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return run_async(_get_messages())


@app.route('/get_messages/id', methods=['GET'])
def get_messages_by_id():
    chat_id = request.args.get('id')
    limit = int(request.args.get('limit', 50))
    
    if not chat_id:
        return jsonify({'error': 'Chat ID required'}), 400
    
    async def _get_messages():
        try:
            client = await cache_manager.get_client()
            entity = await client.get_entity(int(chat_id))
            return await _process_messages(client, entity, limit)
        except Exception as e:
            cached = await cache_manager.get_cached_messages(chat_id)
            if cached:
                return jsonify({'messages': cached, 'cached': True})
            return jsonify({'error': str(e)}), 500
    
    return run_async(_get_messages())


async def _process_messages(client, entity, limit):
    chat_id = entity.id
    chat_name = entity.title if hasattr(entity, 'title') else (entity.first_name or entity.username)
    
    try:
        messages = await client.get_messages(entity, limit=limit)
    except Exception:
        cached = await cache_manager.get_cached_messages(chat_id)
        if cached:
            return jsonify({'messages': cached, 'cached': True})
        raise
    
    result_messages = []
    
    for msg in messages:
        if not msg:
            continue
        
        if msg.sender:
            if isinstance(msg.sender, User):
                sender_name = msg.sender.first_name or msg.sender.username or "Unknown"
            else:
                sender_name = msg.sender.title if hasattr(msg.sender, 'title') else "Channel"
        else:
            sender_name = "Unknown"
        
        message_data = {
            'id': msg.id,
            'sender': sender_name,
            'text': msg.text if msg.text else None,
            'date': msg.date.isoformat() if msg.date else None
        }
        
        if msg.photo:
            asset_id = await cache_manager.download_and_cache_asset(msg, chat_id, msg.id, 'photo')
            message_data['media'] = cache_manager.get_asset_url('photo', asset_id)
        elif hasattr(msg, 'video') and msg.video:
            asset_id = await cache_manager.download_and_cache_asset(msg, chat_id, msg.id, 'video')
            message_data['media'] = cache_manager.get_asset_url('video', asset_id)
        elif hasattr(msg, 'video_note') and msg.video_note:
            asset_id = await cache_manager.download_and_cache_asset(msg, chat_id, msg.id, 'video_note')
            message_data['media'] = cache_manager.get_asset_url('video', asset_id)
        elif hasattr(msg, 'sticker') and msg.sticker:
            asset_id = await cache_manager.download_and_cache_asset(msg, chat_id, msg.id, 'sticker')
            message_data['media'] = cache_manager.get_asset_url('sticker', asset_id)
            message_data['sticker_emoji'] = getattr(msg.sticker, 'emoji', None)
        elif hasattr(msg, 'document') and msg.document:
            asset_id = await cache_manager.download_and_cache_asset(msg, chat_id, msg.id, 'document')
            message_data['media'] = cache_manager.get_asset_url('document', asset_id)
        
        result_messages.append(message_data)
    
    await cache_manager.cache_messages(chat_id, result_messages)
    
    return jsonify({
        'chat_id': chat_id,
        'chat_name': chat_name,
        'messages': result_messages
    })


@app.route('/send_message/username', methods=['POST'])
def send_message_by_username():
    username = request.args.get('username')
    data = request.get_json()
    message = data.get('message') if data else None
    
    if not username or not message:
        return jsonify({'error': 'Username and message required'}), 400
    
    async def _send():
        try:
            client = await cache_manager.get_client()
            entity = await client.get_entity(username)
            sent = await client.send_message(entity, message)
            return jsonify({
                'status': 'sent',
                'message_id': sent.id,
                'date': sent.date.isoformat() if sent.date else None
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return run_async(_send())


@app.route('/send_message/id', methods=['POST'])
def send_message_by_id():
    chat_id = request.args.get('id')
    data = request.get_json()
    message = data.get('message') if data else None
    
    if not chat_id or not message:
        return jsonify({'error': 'Chat ID and message required'}), 400
    
    async def _send():
        try:
            client = await cache_manager.get_client()
            entity = await client.get_entity(int(chat_id))
            sent = await client.send_message(entity, message)
            return jsonify({
                'status': 'sent',
                'message_id': sent.id,
                'date': sent.date.isoformat() if sent.date else None
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return run_async(_send())


@app.route('/get_asset/avatar', methods=['GET'])
def get_avatar():
    user_id = request.args.get('id')
    username = request.args.get('username')
    
    async def _get_avatar():
        try:
            client = await cache_manager.get_client()
            
            if username:
                entity = await client.get_entity(username)
            elif user_id:
                entity = await client.get_entity(int(user_id))
            else:
                return jsonify({'error': 'ID or username required'}), 400
            
            photos = await client.get_profile_photos(entity, limit=1)
            if photos:
                photo_data = BytesIO()
                await client.download_media(photos[0], photo_data)
                photo_data.seek(0)
                return send_file(
                    photo_data,
                    mimetype='image/jpeg',
                    as_attachment=False,
                    download_name=f"avatar_{entity.id}.jpg"
                )
            else:
                return jsonify({'error': 'No avatar found'}), 404
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return run_async(_get_avatar())


@app.route('/get_asset/photo', methods=['GET'])
def get_photo():
    asset_id = request.args.get('asset_id')
    
    if not asset_id:
        return jsonify({'error': 'Asset ID required'}), 400
    
    async def _get_photo():
        data, asset_type = await cache_manager.get_asset_file(asset_id)
        if data:
            return send_file(
                BytesIO(data),
                mimetype='image/jpeg',
                as_attachment=False
            )
        return jsonify({'error': 'Asset not found'}), 404
    
    return run_async(_get_photo())


@app.route('/get_asset/video', methods=['GET'])
def get_video():
    asset_id = request.args.get('asset_id')
    
    if not asset_id:
        return jsonify({'error': 'Asset ID required'}), 400
    
    async def _get_video():
        data, asset_type = await cache_manager.get_asset_file(asset_id)
        
        if asset_type == 'video_large':
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id, message_id FROM large_videos WHERE asset_id = ?", (asset_id,))
            result = cursor.fetchone()
            conn.close()
            
            if result:
                chat_id, message_id = result
                video_data = await cache_manager.download_large_video(asset_id, chat_id, message_id)
                if video_data:
                    return send_file(
                        BytesIO(video_data),
                        mimetype='video/mp4',
                        as_attachment=False
                    )
        elif data:
            return send_file(
                BytesIO(data),
                mimetype='video/mp4',
                as_attachment=False
            )
        
        return jsonify({'error': 'Video not found'}), 404
    
    return run_async(_get_video())


@app.route('/get_asset/sticker', methods=['GET'])
def get_sticker():
    asset_id = request.args.get('asset_id')
    
    if not asset_id:
        return jsonify({'error': 'Asset ID required'}), 400
    
    async def _get_sticker():
        data, asset_type = await cache_manager.get_asset_file(asset_id)
        if data:
            return send_file(
                BytesIO(data),
                mimetype='image/webp',
                as_attachment=False
            )
        return jsonify({'error': 'Sticker not found'}), 404
    
    return run_async(_get_sticker())


@app.route('/get_asset/document', methods=['GET'])
def get_document():
    asset_id = request.args.get('asset_id')
    
    if not asset_id:
        return jsonify({'error': 'Asset ID required'}), 400
    
    async def _get_document():
        data, asset_type = await cache_manager.get_asset_file(asset_id)
        if data:
            return send_file(
                BytesIO(data),
                mimetype='application/octet-stream',
                as_attachment=True
            )
        return jsonify({'error': 'Document not found'}), 404
    
    return run_async(_get_document())


@app.route('/clean_cache', methods=['POST'])
def clean_cache():
    cache_manager.cleanup_cache()
    return jsonify({'status': 'cache_cleaned'})


def main():
    global event_loop
    
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    

    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
