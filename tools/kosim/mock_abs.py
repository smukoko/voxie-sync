import json, os, time
from flask import Flask, request, jsonify
app = Flask(__name__); ST = os.path.join(os.path.dirname(__file__), "abs_state.json")
def load():
    try: return json.load(open(ST))
    except Exception: return {"currentTime": 0.0, "duration": 57449.88, "lastUpdate": 0, "patches": []}
@app.get("/api/me/progress/<iid>")
def get(iid):
    s = load(); return jsonify({"libraryItemId": iid, "currentTime": s["currentTime"], "duration": s["duration"],
                                "progress": s["currentTime"] / s["duration"], "lastUpdate": s["lastUpdate"]})
@app.patch("/api/me/progress/<iid>")
def patch(iid):
    s = load(); body = request.get_json(); s["patches"].append(body); s["currentTime"] = body["currentTime"]; s["lastUpdate"] = int(time.time()*1000)
    json.dump(s, open(ST, "w")); return jsonify({})
@app.get("/api/libraries")
def libs(): return jsonify({"libraries": []})
app.run(host="0.0.0.0", port=13399)
