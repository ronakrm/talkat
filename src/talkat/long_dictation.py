"""Enhanced long dictation module with improved processing and features."""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from typing import Deque, Optional

import numpy as np
import requests

from talkat.config import load_app_config
from talkat.record import stream_audio_with_vad

logger = logging.getLogger(__name__)


@dataclass
class DictationSession:
    """Represents a dictation session with metadata."""
    start_time: datetime
    transcript: list[str]
    word_count: int = 0
    duration: float = 0.0
    pauses: int = 0
    
    def add_text(self, text: str) -> None:
        """Add transcribed text to session."""
        if text:
            self.transcript.append(text)
            self.word_count += len(text.split())
    
    def get_full_text(self) -> str:
        """Get complete transcript as single string."""
        return ' '.join(self.transcript)
    
    def get_stats(self) -> dict:
        """Get session statistics."""
        return {
            "duration_minutes": self.duration / 60,
            "word_count": self.word_count,
            "words_per_minute": self.word_count / (self.duration / 60) if self.duration > 0 else 0,
            "pauses": self.pauses,
            "segments": len(self.transcript)
        }


class EnhancedLongDictation:
    """Enhanced long dictation handler with improved features."""
    
    def __init__(
        self,
        silence_threshold: float = 500.0,
        save_transcripts: bool = True,
        clipboard_on_exit: bool = True,
        auto_save_interval: int = 30,
        show_stats: bool = True
    ):
        self.silence_threshold = silence_threshold
        self.save_transcripts = save_transcripts
        self.clipboard_on_exit = clipboard_on_exit
        self.auto_save_interval = auto_save_interval
        self.show_stats = show_stats
        
        # Session tracking
        self.session = DictationSession(
            start_time=datetime.now(),
            transcript=[]
        )
        
        # Threading and control
        self.stop_event = Event()
        self.auto_save_thread: Optional[Thread] = None
        
        # Transcript management
        self.transcript_dir = Path.home() / ".local/share/talkat/transcripts"
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        
        # Session management
        self.session_file = self.transcript_dir / f"{self.session.start_time.strftime('%Y%m%d_%H%M%S')}_session.json"
        self.transcript_file = self.transcript_dir / f"{self.session.start_time.strftime('%Y%m%d_%H%M%S')}_long.txt"
        
        # HTTP session for efficiency
        self.http_session = requests.Session()
        
        # Signal handling
        signal.signal(signal.SIGINT, self._handle_interrupt)
        signal.signal(signal.SIGTERM, self._handle_interrupt)
    
    def _handle_interrupt(self, signum, frame):
        """Handle interrupt signals gracefully."""
        logger.info("Received interrupt signal, stopping...")
        self.stop_event.set()
    
    def _auto_save_worker(self):
        """Worker thread for auto-saving transcripts."""
        while not self.stop_event.is_set():
            time.sleep(self.auto_save_interval)
            if not self.stop_event.is_set():
                self._save_transcript()
                if self.show_stats:
                    self._print_stats()
    
    def _save_transcript(self):
        """Save current transcript to file."""
        if not self.save_transcripts:
            return
        
        try:
            # Save transcript text
            with open(self.transcript_file, 'w') as f:
                f.write(self.session.get_full_text())
            
            # Save session metadata
            session_data = {
                "start_time": self.session.start_time.isoformat(),
                "duration": self.session.duration,
                "stats": self.session.get_stats(),
                "transcript_file": str(self.transcript_file)
            }
            with open(self.session_file, 'w') as f:
                json.dump(session_data, f, indent=2)
            
            logger.debug(f"Auto-saved transcript to {self.transcript_file}")
        except Exception as e:
            logger.error(f"Failed to auto-save transcript: {e}")
    
    def _print_stats(self):
        """Print current session statistics."""
        stats = self.session.get_stats()
        print(f"\r[{stats['duration_minutes']:.1f}m | "
              f"{stats['word_count']} words | "
              f"{stats['words_per_minute']:.0f} wpm | "
              f"{stats['segments']} segments]", end='', flush=True)
    
    def _copy_to_clipboard(self, text: str):
        """Copy text to system clipboard."""
        if not text:
            return False
        
        try:
            # Try wl-copy (Wayland)
            subprocess.run(
                ['wl-copy'],
                input=text.encode('utf-8'),
                check=True,
                capture_output=True
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            try:
                # Fallback to xclip (X11)
                subprocess.run(
                    ['xclip', '-selection', 'clipboard'],
                    input=text.encode('utf-8'),
                    check=True,
                    capture_output=True
                )
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                logger.warning("Could not copy to clipboard (wl-copy/xclip not found)")
                return False
    
    def _send_notification(self, title: str, message: str):
        """Send desktop notification."""
        try:
            subprocess.run(
                ['notify-send', title, message],
                check=False,
                capture_output=True
            )
        except FileNotFoundError:
            pass
    
    def _transcribe_audio(self, audio_generator, sample_rate: int) -> Optional[str]:
        """Send audio to server for transcription."""
        def request_data_generator():
            # Send metadata first
            metadata = json.dumps({"rate": sample_rate})
            yield metadata.encode('utf-8') + b'\n'
            
            # Stream audio chunks
            for chunk in audio_generator:
                if isinstance(chunk, int):
                    continue  # Skip sample rate
                yield chunk
        
        server_url = "http://127.0.0.1:5555/transcribe_stream"
        
        try:
            response = self.http_session.post(
                server_url,
                data=request_data_generator(),
                timeout=120
            )
            response.raise_for_status()
            
            result = response.json()
            return result.get('text', '').strip()
            
        except requests.ConnectionError:
            logger.error("Cannot connect to model server")
            print("\nError: Model server is not running. Start it with: talkat server")
            return None
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return None
    
    def run(self) -> int:
        """Run the enhanced long dictation mode."""
        print("Enhanced Long Dictation Mode")
        print("=" * 40)
        print("Press Ctrl+C to stop and save")
        print("")
        
        # Check server health
        try:
            health = self.http_session.get("http://127.0.0.1:5555/health", timeout=2)
            if health.status_code != 200:
                print("Error: Model server is not ready")
                return 1
        except requests.ConnectionError:
            print("Error: Model server is not running")
            print("Start it with: talkat server")
            return 1
        
        # Start auto-save thread
        if self.auto_save_interval > 0:
            self.auto_save_thread = Thread(target=self._auto_save_worker, daemon=True)
            self.auto_save_thread.start()
        
        # Send start notification
        self._send_notification("Talkat", "Long dictation started")
        
        print("Listening... Speak naturally with pauses between sentences.")
        print("")
        
        consecutive_empty = 0
        max_consecutive_empty = 3
        
        try:
            while not self.stop_event.is_set():
                # Stream audio with VAD
                audio_generator = stream_audio_with_vad(
                    silence_threshold=self.silence_threshold,
                    silence_duration=1.5,  # Longer pause tolerance for long dictation
                    debug=False,
                    max_duration=600.0  # 10 minute max per segment
                )
                
                # Get sample rate
                sample_rate = next(audio_generator)
                
                # Transcribe the audio stream
                text = self._transcribe_audio(audio_generator, sample_rate)
                
                if text:
                    self.session.add_text(text)
                    consecutive_empty = 0
                    
                    # Display transcribed text
                    print(f"\n{text}")
                    
                    # Update duration
                    self.session.duration = (datetime.now() - self.session.start_time).total_seconds()
                    
                    if self.show_stats:
                        self._print_stats()
                else:
                    consecutive_empty += 1
                    self.session.pauses += 1
                    
                    if consecutive_empty >= max_consecutive_empty:
                        print("\nNo speech detected for a while. Still listening...")
                        consecutive_empty = 0
                
                # Check for stop signal
                if self.stop_event.is_set():
                    break
                    
        except KeyboardInterrupt:
            pass
        finally:
            print("\n\nStopping long dictation...")
            
            # Clean up
            self.stop_event.set()
            self.http_session.close()
            
            # Final save
            self._save_transcript()
            
            # Get final text
            full_text = self.session.get_full_text()
            
            # Copy to clipboard if requested
            if self.clipboard_on_exit and full_text:
                if self._copy_to_clipboard(full_text):
                    print("Transcript copied to clipboard")
            
            # Show final statistics
            if self.show_stats:
                print("\nSession Summary:")
                print("-" * 40)
                stats = self.session.get_stats()
                print(f"Duration: {stats['duration_minutes']:.1f} minutes")
                print(f"Words: {stats['word_count']}")
                print(f"Average WPM: {stats['words_per_minute']:.0f}")
                print(f"Segments: {stats['segments']}")
                print(f"Pauses: {stats['pauses']}")
            
            if self.save_transcripts:
                print(f"\nTranscript saved to: {self.transcript_file}")
                print(f"Session data saved to: {self.session_file}")
            
            # Send completion notification
            self._send_notification(
                "Talkat",
                f"Long dictation complete. {self.session.word_count} words transcribed."
            )
            
            return 0


def run_enhanced_long_dictation(
    silence_threshold: Optional[float] = None,
    save_transcripts: bool = True,
    clipboard: bool = True,
    auto_save: int = 30,
    show_stats: bool = True
) -> int:
    """Run enhanced long dictation mode with improved features."""
    config = load_app_config()
    
    if silence_threshold is None:
        silence_threshold = config.get('silence_threshold', 500.0)
    
    save_transcripts = config.get('save_transcripts', save_transcripts)
    clipboard = config.get('clipboard_on_long', clipboard)
    
    dictation = EnhancedLongDictation(
        silence_threshold=silence_threshold,
        save_transcripts=save_transcripts,
        clipboard_on_exit=clipboard,
        auto_save_interval=auto_save,
        show_stats=show_stats
    )
    
    return dictation.run()