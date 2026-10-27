# Import libraries
import base64
import hashlib
import http.server
import json
import os
import urllib.parse
import webbrowser
import requests

# Information needed to access and locate the API and be able to pull it
CLIENT_ID = "YOUR_CLIENT_ID"
CLIENT_SECRET = "YOUR_CLIENT_SECRET"

REDIRECT_URI = "http://localhost:8080/callback"
AUTH_HOST = "auth.us.mymojohealth.com"
API_HOST = "api.us.mymojohealth.com"
TOKEN_FILE = "tokens.json"



# Create PCKE to protect the authorization process
def generate_pkce_pairs():
  code_verifier = (
      base64.urlsafe_b64encode(os.urandom(96)).rstrip(b"=").decode("ascii")
  )
  hashed = hashlib.sha256(code_verifier.encode("ascii")).digest()
  code_challenge = (
      base64.urlsafe_b64encode(hashed).rstrip(b"=").decode("ascii")
  )
  return code_verifier, code_challenge

# Holds the classification code and holds it determine whether the process can continue
class CallbackHandler(http.server.BaseHTTPRequestHandler):
  auth_code = None

  def do_GET(self):
    parsed_path = urllib.parse.urlparse(self.path)
    query_params = urllib.parse.parse_qs(parsed_path.query)
    if "code" in query_params:
      CallbackHandler.auth_code = query_params["code"][0]
      self.send_response(200)
      self.send_header("Content-type", "text/html")
      self.end_headers()
      self.wfile.write(
          b"<h2>Authentication successful! Return to Terminal.</h2>"
      )
    else:
      self.send_response(400)
      self.end_headers()
      self.wfile.write(b"<h2>Authorization failed or denied.</h2>")

  def log_message(self, format, *args):
    pass

# Initializes the parameters and runs the automation process
def run_auth_flow():
  code_verifier, code_challenge = generate_pkce_pairs()

  params = {
      "response_type": "code",
      "client_id": CLIENT_ID.strip(),
      "code_challenge_method": "S256",
      "code_challenge": code_challenge,
      "redirect_uri": REDIRECT_URI,
      "scope": "readings:retrieve users:profile:retrieve",
  }

  auth_url = f"https://{AUTH_HOST}/oauth/authorize?{urllib.parse.urlencode(params)}"

  print("Opening browser for MyMojoHealth authentication...")
  webbrowser.open(auth_url)

  server = http.server.HTTPServer(("localhost", 8080), CallbackHandler)
  print("Waiting for callback on http://localhost:8080/callback ...")
  while CallbackHandler.auth_code is None:
    server.handle_request()

  auth_code = CallbackHandler.auth_code
  print("Captured Auth Code! Swapping for tokens on correct endpoint...")

  token_url = f"https://{AUTH_HOST}/api/v1/oauth/token"
  payload = {
      "grant_type": "authorization_code",
      "client_id": CLIENT_ID.strip(),
      "client_secret": CLIENT_SECRET.strip(),
      "redirect_uri": REDIRECT_URI,
      "code": auth_code,
      "code_verifier": code_verifier,
  }

  headers = {"Content-Type": "application/x-www-form-urlencoded"}
  response = requests.post(token_url, data=payload, headers=headers)
  response.raise_for_status()

  tokens = response.json()
  with open(TOKEN_FILE, "w") as f:
    json.dump(tokens, f, indent=2)

  print(f"\nSuccess! Tokens stored in '{TOKEN_FILE}'.")


if __name__ == "__main__":
  run_auth_flow()