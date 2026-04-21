import asyncio
import base64
import os
import configparser
from quart import Quart, request, jsonify
from quart_cors import cors
from telethon import TelegramClient
from telethon.sessions import StringSession

app = Quart(__name__)
app = cors(app, allow_origin="*")


config = configparser.ConfigParser()
config.read('settings.inf')

API_ID = int(config['Telegram']['api_id'])
API_HASH = config['Telegram']['api_hash']

print(f"API ID: {API_ID}")
print(f"API Hash: {API_HASH[:10]}...")

# Хранилище временных данных
auth_flows = {}

@app.route('/telegram/login/phone/1', methods=['GET'])
async def login_phone():
    phone = request.args.get('num')
    print(f"Login phone request: {phone}")
    
    if not phone:
        return jsonify({'success': False, 'error': 'Phone number required'})
    
    temp_id = os.urandom(16).hex()
    
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        auth_flows[temp_id] = {
            'phone': phone,
            'phone_code_hash': result.phone_code_hash,
            'client': client
        }
        print(f"Code sent to {phone}, temp_id: {temp_id}")
        
        return jsonify({
            'success': True,
            'data': {
                'message': 'Code sent',
                'temp_id': temp_id
            }
        })
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/telegram/login/phone/2', methods=['GET'])
async def login_code():
    code = request.args.get('code')
    temp_id = request.args.get('temp_id')
    
    print(f"Login code request: code={code}, temp_id={temp_id}")
    
    if not code or not temp_id:
        return jsonify({'success': False, 'error': 'Code and temp_id required'})
    
    flow = auth_flows.get(temp_id)
    if not flow:
        return jsonify({'success': False, 'error': 'Auth session not found'})
    
    client = flow['client']
    
    try:
        await client.sign_in(
            phone=flow['phone'],
            code=code,
            phone_code_hash=flow['phone_code_hash']
        )
        
        me = await client.get_me()
        session_string = client.session.save()
        session_base64 = base64.b64encode(session_string.encode()).decode()
        
        await client.disconnect()
        del auth_flows[temp_id]
        
        print(f"Login successful for {me.phone}")
        
        return jsonify({
            'success': True,
            'data': {
                'session_data': session_base64,
                'message': 'Login successful'
            }
        })
    except Exception as e:
        print(f"Sign in error: {e}")
        await client.disconnect()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/telegram/get_me', methods=['POST'])
async def get_me():
    data = await request.get_json()
    session_base64 = data.get('session', {}).get('data')
    
    if not session_base64:
        return jsonify({'success': False, 'error': 'Session required'})
    
    session_string = base64.b64decode(session_base64).decode()
    
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        me = await client.get_me()
        await client.disconnect()
        
        return jsonify({
            'success': True,
            'data': {
                'ID': me.id,
                'FirstName': me.first_name or '',
                'LastName': me.last_name or '',
                'Username': me.username or '',
                'Phone': me.phone or ''
            }
        })
    except Exception as e:
        print(f"Get me error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/telegram/send_message', methods=['POST'])
async def send_message():
    data = await request.get_json()
    session_base64 = data.get('session', {}).get('data')
    peer = data.get('peer')
    message = data.get('message')
    
    if not all([session_base64, peer, message]):
        return jsonify({'success': False, 'error': 'Missing fields'})
    
    session_string = base64.b64decode(session_base64).decode()
    
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        entity = await client.get_entity(peer)
        await client.send_message(entity, message)
        await client.disconnect()
        return jsonify({'success': True})
    except Exception as e:
        print(f"Send error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/telegram/get_dialogs', methods=['POST'])
async def get_dialogs():
    """Получение списка диалогов с полной информацией"""
    data = await request.get_json()
    session_base64 = data.get('session', {}).get('data')
    limit = data.get('limit', 100)
    
    if not session_base64:
        return jsonify({'success': False, 'error': 'Session required'})
    
    session_string = base64.b64decode(session_base64).decode()
    
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        dialogs = await client.get_dialogs(limit=limit)
        await client.disconnect()
        
        result = []
        for dialog in dialogs:
            entity = dialog.entity
            
            # Определяем peer (username или ID)
            peer = None
            peer_type = 'unknown'
            
            if hasattr(entity, 'username') and entity.username:
                peer = f"@{entity.username}"
                peer_type = 'username'
            elif hasattr(entity, 'id'):
                # Для каналов ID обычно отрицательный
                peer = str(entity.id)
                peer_type = 'id'
            
            # Определяем тип чата
            chat_type = 'user'
            if dialog.is_channel:
                chat_type = 'channel'
            elif dialog.is_group:
                chat_type = 'group'
            elif dialog.is_user:
                chat_type = 'user'
            
            result.append({
                'id': dialog.id,
                'name': dialog.name,
                'peer': peer,  # Основное поле для использования в get_messages
                'peer_id': entity.id if hasattr(entity, 'id') else None,
                'peer_type': peer_type,
                'chat_type': chat_type,
                'unread': dialog.unread_count,
                'message': {
                    'text': dialog.message.text if dialog.message else None,
                    'date': dialog.message.date.timestamp() if dialog.message and dialog.message.date else None
                } if dialog.message else None
            })
        
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        print(f"Dialogs error: {e}")
        return jsonify({'success': False, 'error': str(e)})
        
@app.route('/telegram/get_photo', methods=['POST'])
async def get_photo():
    """Скачивание фото пользователя по ID"""
    data = await request.get_json()
    session_base64 = data.get('session', {}).get('data')
    user_id = data.get('user_id')
    
    if not session_base64:
        return jsonify({'success': False, 'error': 'Session required'})
    
    if not user_id:
        return jsonify({'success': False, 'error': 'User ID required'})
    
    session_string = base64.b64decode(session_base64).decode()
    
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        
        # Получаем пользователя по ID
        try:
            entity = await client.get_entity(int(user_id))
        except Exception as e:
            await client.disconnect()
            return jsonify({'success': False, 'error': f'User not found: {str(e)}'})
        
        # Скачиваем фото
        photo_base64 = None
        if hasattr(entity, 'photo') and entity.photo:
            try:
                file = await client.download_profile_photo(entity, bytesIO=True)
                if file:
                    photo_base64 = base64.b64encode(file.getvalue()).decode()
            except Exception as e:
                print(f"Photo download error: {e}")
        
        await client.disconnect()
        
        if photo_base64:
            return jsonify({'success': True, 'data': {'photo_base64': photo_base64}})
        else:
            return jsonify({'success': False, 'error': 'No photo available'})
    except Exception as e:
        print(f"Get photo error: {e}")
        return jsonify({'success': False, 'error': str(e)})
        
        
@app.route('/telegram/get_user_info', methods=['POST'])
async def get_user_info():
    """Получение информации о пользователе по username (имя + фото профиля)"""
    data = await request.get_json()
    session_base64 = data.get('session', {}).get('data')
    username = data.get('username', '').strip()
    
    if not session_base64:
        return jsonify({'success': False, 'error': 'Session required'})
    
    if not username:
        return jsonify({'success': False, 'error': 'Username required'})
    
    # Убираем @ если есть
    if username.startswith('@'):
        username = username[1:]
    
    session_string = base64.b64decode(session_base64).decode()
    
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        
        # Получаем информацию о пользователе
        try:
            entity = await client.get_entity(username)
        except Exception as e:
            await client.disconnect()
            return jsonify({'success': False, 'error': f'User not found: {str(e)}'})
        
        # Получаем фото профиля
        photo_base64 = None
        photo_info = None
        
        if hasattr(entity, 'photo') and entity.photo:
            try:
                # Получаем файл фото
                file = await client.download_profile_photo(entity, bytesIO=True)
                if file:
                    photo_base64 = base64.b64encode(file.getvalue()).decode()
                    photo_info = {
                        'has_photo': True,
                        'photo_base64': photo_base64,
                        'photo_size': len(file.getvalue())
                    }
                else:
                    photo_info = {'has_photo': False, 'reason': 'Could not download photo'}
            except Exception as e:
                print(f"Photo download error: {e}")
                photo_info = {'has_photo': False, 'reason': str(e)}
        else:
            photo_info = {'has_photo': False, 'reason': 'User has no profile photo'}
        
        # Формируем результат
        result = {
            'id': entity.id,
            'first_name': getattr(entity, 'first_name', ''),
            'last_name': getattr(entity, 'last_name', ''),
            'username': getattr(entity, 'username', ''),
            'phone': getattr(entity, 'phone', ''),
            'is_bot': getattr(entity, 'bot', False),
            'is_verified': getattr(entity, 'verified', False),
            'is_scam': getattr(entity, 'scam', False),
            'is_premium': getattr(entity, 'premium', False),
            'photo': photo_info
        }
        
        await client.disconnect()
        
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        print(f"Get user info error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/telegram/get_messages', methods=['POST'])
async def get_messages():
    data = await request.get_json()
    session_base64 = data.get('session', {}).get('data')
    peer = data.get('peer')
    limit = data.get('limit', 50)
    
    if not all([session_base64, peer]):
        return jsonify({'success': False, 'error': 'Missing fields'})
    
    session_string = base64.b64decode(session_base64).decode()
    
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        entity = await client.get_entity(peer)
        messages = await client.get_messages(entity, limit=limit)
        await client.disconnect()
        
        result = []
        for msg in messages:
            result.append({
                'id': msg.id,
                'text': msg.text or '',
                'date': msg.date.timestamp() if msg.date else 0,
                'out': msg.out
            })
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        print(f"Messages error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/health', methods=['GET'])
async def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    print(f"Starting server on http://localhost:8080")
    app.run(host='0.0.0.0', port=8080, debug=True)
