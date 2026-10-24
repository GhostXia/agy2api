"""Minimal PySide6 client for agy2api.

Ponytail MVP: Simple prompt input -> sends to local agy2api -> displays response.
Zero external dependencies beyond PySide6.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

SERVER_URL = os.environ.get("AGY2API_URL", "http://127.0.0.1:7862/v1/chat/completions")
API_KEY = os.environ.get("AGY2API_KEY", "pwd")
DEFAULT_MODEL = os.environ.get("AGY2API_MODEL", "claude-sonnet-4.6")


class SendWorker(QThread):
    """Background worker sending request to agy2api without freezing the GUI."""

    success = Signal(str)
    failure = Signal(str)

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def run(self) -> None:
        payload = {
            "model": DEFAULT_MODEL,
            "messages": [{"role": "user", "content": self.prompt}],
            "stream": False,
        }
        req = urllib.request.Request(
            SERVER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                reply = data["choices"][0]["message"]["content"]
                self.success.emit(reply)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            self.failure.emit(f"HTTP {exc.code}: {err_body or exc.reason}")
        except Exception as exc:
            self.failure.emit(f"خطأ في الاتصال: {exc}")


class AgyClientWindow(QMainWindow):
    """Main window with accessible controls and logical tab order."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("عميل agy2api البسيط")
        self.resize(650, 550)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)

        # 1. Prompt Input
        lbl_prompt = QLabel("اكتب طلبك هنا:", central)
        self.txt_prompt = QPlainTextEdit(central)
        self.txt_prompt.setAccessibleName("حقل إدخال الطلب")
        self.txt_prompt.setPlaceholderText("أدخل السؤال أو التعليمات هنا...")
        lbl_prompt.setBuddy(self.txt_prompt)
        layout.addWidget(lbl_prompt)
        layout.addWidget(self.txt_prompt, stretch=2)

        # 2. Send Button
        self.btn_send = QPushButton("إرسال (Ctrl+Enter)", central)
        self.btn_send.setAccessibleName("زر إرسال الطلب إلى الخادم")
        self.btn_send.clicked.connect(self._on_send)
        layout.addWidget(self.btn_send)

        # 3. Output Response
        lbl_response = QLabel("الإجابة المستلمة:", central)
        self.txt_response = QPlainTextEdit(central)
        self.txt_response.setReadOnly(True)
        self.txt_response.setAccessibleName("حقل عرض الإجابة")
        self.txt_response.setPlaceholderText("ستظهر إجابة النموذج هنا...")
        lbl_response.setBuddy(self.txt_response)
        layout.addWidget(lbl_response)
        layout.addWidget(self.txt_response, stretch=3)

        self.setCentralWidget(central)

        # Keyboard shortcuts
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._on_send)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._on_send)

        # Strict logical tab order for screen readers
        self.setTabOrder(self.txt_prompt, self.btn_send)
        self.setTabOrder(self.btn_send, self.txt_response)

        self._worker: SendWorker | None = None

    def _on_send(self) -> None:
        prompt = self.txt_prompt.toPlainText().strip()
        if not prompt:
            return

        self.btn_send.setEnabled(False)
        self.btn_send.setText("جارٍ المعالجة والإرسال...")
        self.txt_response.setPlainText("انتظار رد النموذج من خادم agy2api...")

        self._worker = SendWorker(prompt)
        self._worker.success.connect(self._on_success)
        self._worker.failure.connect(self._on_failure)
        self._worker.start()

    def _on_success(self, reply: str) -> None:
        self.txt_response.setPlainText(reply)
        self.btn_send.setEnabled(True)
        self.btn_send.setText("إرسال (Ctrl+Enter)")
        self.txt_response.setFocus()

    def _on_failure(self, err_msg: str) -> None:
        self.txt_response.setPlainText(f"تعذر الحصول على رد:\n{err_msg}")
        self.btn_send.setEnabled(True)
        self.btn_send.setText("إرسال (Ctrl+Enter)")


def main() -> None:
    app = QApplication(sys.argv)
    win = AgyClientWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
