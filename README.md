# Web Chat Application

## Description
This is a real-time web chat application built using **Flask**, **Flask-SocketIO**, and **SQLite**.  
It allows multiple users to register, log in, and chat live through a browser interface.  
Messages are updated instantly using WebSockets.

## Language & Dependencies
- **Language:** Python  
- **Frameworks/Libraries:** Flask, Flask-SocketIO, Eventlet, SQLite3  
- **Dependency Manager:** pip (Python package manager)

## Installation
Instructions for installing dependencies:
```bash
# Clone this repository
git clone https://github.com/manya-ahuja20/web_chat_app.git
cd web_chat_app

# Create a virtual environment (optional but recommended)
python -m venv venv
venv\Scripts\activate   # On Windows

# Install all required dependencies
pip install -r requirements.txt
```

## Usage
How to run your project:
```bash
# Run the Flask app
python src/app.py

```
Then open http://localhost:5000
 in your browser for server.
 And local IPv4 address for clients.

## Authors
Manya Ahuja