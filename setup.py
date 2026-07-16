import subprocess
import sys


print("Installing Omar AI Core requirements...")
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)
print("Setup complete. Run: python -m omar_ai_core")
