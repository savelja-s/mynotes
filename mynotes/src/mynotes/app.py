from pathlib import Path
import asyncio
import json
import pickle
import toga
from toga.style import Pack
from toga.style.pack import COLUMN
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive.metadata.readonly']
APP_DIR = Path.home() / ".mynotes"
APP_DIR.mkdir(parents=True, exist_ok=True)
SECRET_FILE = APP_DIR / "client_secret.json"
TOKEN_FILE = APP_DIR / "token.pickle"
FOLDER_NAME = "MyGCNotesFolder"


class GNotesApp(toga.App):
    class SampleDialog(toga.Window):
        def __init__(self):
            super().__init__(title="Нова нотатка", size=(400, 200), resizable=False)

            self.name_input = toga.TextInput(placeholder="Назва нотатки", style=Pack(margin=(5, 0)))
            self.desc_input = toga.TextInput(placeholder="Опис нотатки", style=Pack(margin=(5, 0)))

            self.ok_button = toga.Button("OK", on_press=self.on_accept)
            self.cancel_button = toga.Button("Скасувати", on_press=self.on_cancel)

            self.future = self.app.loop.create_future()

            button_box = toga.Box(
                children=[self.ok_button, self.cancel_button],
                style=Pack(direction='row')
            )

            box = toga.Box(
                children=[self.name_input, self.desc_input, button_box],
                style=Pack(direction=COLUMN, margin=10)
            )

            self.content = box

        def on_accept(self, widget):
            self.future.set_result({
                'name': self.name_input.value,
                'description': self.desc_input.value
            })
            self.close()

        def on_cancel(self, widget):
            self.future.set_result(None)
            self.close()

        def __await__(self):
            self.show()
            return self.future.__await__()

    async def force_relogin(self, widget):
        SECRET_FILE.unlink(missing_ok=True)
        TOKEN_FILE.unlink(missing_ok=True)
        self.creds = None
        self.service = None
        await self.login_to_google(widget)

    def startup(self):
        self.creds = None
        self.service = None
        self.notes_folder_id = None

        self.main_box = toga.Box(style=Pack(direction=COLUMN, margin=10))

        self.login_button = toga.Button(
            "🔑 Увійти в Google", on_press=self.login_to_google, style=Pack(margin=10)
        )
        self.relogin_button = toga.Button(
            "🔄 Змінити обліковку", on_press=self.force_relogin, style=Pack(margin=10)
        )
        self.add_note_button = toga.Button(
            "➕ Додати нотатку", on_press=self.show_add_note_dialog, style=Pack(margin=10), enabled=False
        )
        self.notes_label = toga.Label("📄 Нотатки з Google Drive", style=Pack(margin=10))
        self.notes_list = toga.MultilineTextInput(readonly=True, style=Pack(flex=1, margin=10))

        self.main_box.add(self.login_button)
        self.main_box.add(self.relogin_button)
        self.main_box.add(self.add_note_button)
        self.main_box.add(self.notes_label)
        self.main_box.add(self.notes_list)

        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = self.main_box
        self.main_window.show()

        if TOKEN_FILE.exists() and SECRET_FILE.exists():
            if self.load_credentials():
                self.init_service()
                asyncio.ensure_future(self.ensure_notes_folder_and_load_files())
                self.add_note_button.enabled = True

    def load_credentials(self):
        try:
            with open(TOKEN_FILE, 'rb') as token:
                self.creds = pickle.load(token)
            if self.creds and self.creds.valid:
                return True
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
                self.save_credentials()
                return True
        except Exception:
            pass
        self.creds = None
        return False

    def save_credentials(self):
        with open(TOKEN_FILE, 'wb') as token:
            pickle.dump(self.creds, token)

    async def login_to_google(self, widget):
        if not SECRET_FILE.exists():
            await self.main_window.info_dialog("Файл не знайдено", "Оберіть client_secret.json")
            success = await self.select_and_save_secret_file()
            if not success:
                await self.main_window.error_dialog("Помилка", "Файл не обрано")
                return

        flow = InstalledAppFlow.from_client_secrets_file(str(SECRET_FILE), SCOPES)
        self.creds = flow.run_local_server(port=8080)
        self.save_credentials()
        self.init_service()
        await self.ensure_notes_folder_and_load_files()
        self.add_note_button.enabled = True

    async def select_and_save_secret_file(self):
        file_path = await self.main_window.open_file_dialog("Оберіть client_secret.json")

        if file_path and file_path.name.endswith(".json"):
            content = file_path.read_bytes()
            SECRET_FILE.write_bytes(content)
            return True
        return False

    def init_service(self):
        self.service = build('drive', 'v3', credentials=self.creds)

    async def ensure_notes_folder_and_load_files(self):
        folder_id = self.find_folder_id_by_name(FOLDER_NAME)
        if not folder_id:
            folder_id = self.create_notes_folder()
        self.notes_folder_id = folder_id
        await self.load_notes()

    def find_folder_id_by_name(self, folder_name):
        query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
        results = self.service.files().list(q=query, spaces='drive', fields="files(id, name)").execute()
        files = results.get('files', [])
        if files:
            return files[0]['id']
        return None

    def create_notes_folder(self):
        file_metadata = {
            'name': FOLDER_NAME,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = self.service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')

    async def load_notes(self):
        query = f"'{self.notes_folder_id}' in parents and trashed=false and name contains '.json'"
        results = self.service.files().list(q=query, spaces='drive', fields="files(id, name)").execute()
        items = results.get('files', [])

        if not items:
            self.notes_list.value = "Нотаток не знайдено"
        else:
            notes_text = "\n".join(f"- {item['name']}" for item in items)
            self.notes_list.value = notes_text

    async def show_add_note_dialog(self, widget):
        dialog = self.SampleDialog()
        result = await dialog
        if result:
            note_data = {
                "name": result['name'],
                "context": "Новий запис",
                "description": result['description']
            }
            self.create_note_on_drive(note_data)

    def create_note_on_drive(self, note_data):
        if not self.creds or not self.service or not self.notes_folder_id:
            self.main_window.error_dialog("Помилка", "Вам потрібно увійти в Google")
            return

        file_name = note_data['name']
        content = {
            "context": note_data["context"],
            "description": note_data["description"]
        }
        json_content = json.dumps(content, ensure_ascii=False).encode('utf-8')

        file_metadata = {
            'name': f"{file_name}.json",
            'parents': [self.notes_folder_id],
            'mimeType': 'application/json'
        }
        media = MediaInMemoryUpload(json_content, mimetype='application/json')

        try:
            created_file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, name'
            ).execute()

            self.main_window.info_dialog("Успіх", f"Файл '{created_file['name']}' створено")
            asyncio.ensure_future(self.load_notes())

        except Exception as e:
            self.main_window.error_dialog("Не вдалося створити файл", str(e))


def main():
    return GNotesApp()
