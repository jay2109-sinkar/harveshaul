from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import json, os, uuid, hashlib
from datetime import datetime, timedelta
from functools import wraps
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import time

# LLM integration
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__, static_folder='static', template_folder='.')
app.secret_key = "supersecretkey"  # In production, use a secure random key and keep it secret

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LISTINGS_FILE = os.path.join(DATA_DIR, "listings.json")
DEMANDS_FILE = os.path.join(DATA_DIR, "demands.json")
MATCHES_FILE = os.path.join(DATA_DIR, "matches.json")
DRIVERS_FILE = os.path.join(DATA_DIR, "drivers.json")
GEOCACHE_FILE = os.path.join(DATA_DIR, "geocache.json")
IMPACT_FILE = os.path.join(DATA_DIR, "impact.json")
CHAT_HISTORY_FILE = os.path.join(DATA_DIR, "chat_history.json")

# Configuration constants for scoring and estimates
AVG_SPEED_KMH = 35.0  # conservative urban average
HANDLING_HOURS = 0.5  # pickup/dropoff buffer
MEAL_KG = 0.4         # average kg per meal
CO2_PER_KG = 2.5      # estimated kg CO2 avoided per kg rescued
DEFAULT_GEOCODER_COUNTRY = "in"

# Initialize geolocator (Nominatim uses OpenStreetMap)
GEOCODER = Nominatim(user_agent="harvesthaul_agent")

# ── Gemini LLM Setup ────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
gemini_client = None

if GEMINI_API_KEY and GEMINI_API_KEY != "your-api-key-here":
    try:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("✅ Gemini LLM connected successfully")
    except Exception as e:
        print(f"⚠️ Gemini setup failed: {e}")

SYSTEM_PROMPT = """You are HarvestHaul AI — a friendly, knowledgeable assistant for the HarvestHaul food rescue platform.

HarvestHaul connects food donors (restaurants, farms, supermarkets) with NGOs (shelters, food banks, community kitchens) through intelligent multi-agent coordination. It rescues surplus food before it spoils and routes it to those who need it most.

Your role:
• Help users create food listings (as donors) or register food needs (as NGOs)
• Provide platform statistics and impact metrics
• Run the matchmaker to pair food with NGOs
• Give food safety and storage advice
• Answer questions about how HarvestHaul works

Guidelines:
• Be warm, concise, and action-oriented
• When a user describes food they want to donate, extract details and use create_listing
• When a user describes a food need, extract details and use create_demand
• Use appropriate units (kg, lbs, litres, portions, boxes) — default to kg if unclear
• Default expiry_hours to 24 if not specified
• Always confirm actions taken with a brief summary
• Use emoji sparingly for key items (✅ for success, 📦 for food, 🌍 for impact)
• If the user's request is ambiguous, ask a clarifying question
• You are embedded in the HarvestHaul web dashboard"""

def _build_gemini_tools():
    """Build Gemini function declarations from MCP tool definitions."""
    from google.genai import types
    from mcp_server import TOOL_DEFINITIONS
    
    declarations = []
    for tool_def in TOOL_DEFINITIONS:
        # Build properties schema
        props = {}
        required = tool_def["parameters"].get("required", [])
        
        for param_name, param_schema in tool_def["parameters"].get("properties", {}).items():
            schema_args = {}
            ptype = param_schema.get("type", "string")
            
            if ptype == "string":
                schema_args["type"] = "STRING"
            elif ptype == "number":
                schema_args["type"] = "NUMBER"
            elif ptype == "integer":
                schema_args["type"] = "INTEGER"
            elif ptype == "boolean":
                schema_args["type"] = "BOOLEAN"
            elif ptype == "array":
                schema_args["type"] = "ARRAY"
                items_type = param_schema.get("items", {}).get("type", "string").upper()
                schema_args["items"] = types.Schema(type=items_type)
            else:
                schema_args["type"] = "STRING"
            
            if "description" in param_schema:
                schema_args["description"] = param_schema["description"]
            if "enum" in param_schema:
                schema_args["enum"] = param_schema["enum"]
            
            props[param_name] = types.Schema(**schema_args)
        
        decl = types.FunctionDeclaration(
            name=tool_def["name"],
            description=tool_def["description"],
            parameters=types.Schema(
                type="OBJECT",
                properties=props,
                required=required
            ) if props else None
        )
        declarations.append(decl)
    
    return types.Tool(function_declarations=declarations)


def load_json(path, default=None):
    if default is None:
        default = []
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def ensure_user_session():
    if "user_id" not in session:
        session["user_id"] = "guest-user"
        session["user_email"] = "guest@example.com"
        session["user_name"] = "Guest User"
        session["user_role"] = "donor"
    return session

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ensure_user_session()
        return f(*args, **kwargs)
    return decorated

def send_notification(match):
    NOTES_FILE = os.path.join(DATA_DIR, "notifications.json")
    notes = load_json(NOTES_FILE, [])
    note = {
        "id": str(uuid.uuid4()),
        "match_id": match["id"],
        "to": match.get("driver", "Volunteer"),
        "message": f"Pickup {match.get('quantity')} {match.get('unit')} of {match.get('food_type')} from {match.get('pickup_location')} -> {match.get('dropoff_location')}. ETA {match.get('eta')}",
        "created": datetime.now().isoformat()
    }
    notes.append(note)
    save_json(NOTES_FILE, notes)
    print("[notify]", note)


### --- Helpers: geocoding, distance, scoring, impact ---
def get_geocache():
    return load_json(GEOCACHE_FILE, {})

def save_geocache(cache):
    save_json(GEOCACHE_FILE, cache)

def normalize_address(address):
    if not address:
        return ""
    clean = " ".join(address.strip().split())
    return clean

def get_coords(address):
    """Return (lat, lon) for an address using cache and Nominatim.
       Returns None on failure."""
    if not address:
        return None
    address = normalize_address(address)
    cache = get_geocache()
    cache_key = address.lower()
    if cache_key in cache:
        return tuple(cache[cache_key])
    try:
        # respectful delay to avoid throttling
        time.sleep(1)
        loc = GEOCODER.geocode(address, timeout=10, exactly_one=True, country_codes=DEFAULT_GEOCODER_COUNTRY)
        if not loc:
            loc = GEOCODER.geocode(f"{address}, India", timeout=10, exactly_one=True)
        if not loc:
            loc = GEOCODER.geocode(address, timeout=10, exactly_one=True)
        if loc:
            coords = (loc.latitude, loc.longitude)
            cache[cache_key] = coords
            save_geocache(cache)
            return coords
    except Exception as e:
        print("Geocode error for", address, e)
    return None

def calc_distance_km(a1, a2):
    """Calculate distance in km between two address strings. Falls back to 10 km if geocoding fails."""
    try:
        c1 = get_coords(a1)
        c2 = get_coords(a2)
        if c1 and c2:
            return geodesic(c1, c2).km
    except Exception as e:
        print("Distance calc error:", e)
    return 10.0

def calc_spoilage_risk(expiry_hours, distance_km):
    """Estimate delivery hours then map to risk level."""
    delivery_hours = distance_km / AVG_SPEED_KMH + HANDLING_HOURS
    remaining = expiry_hours - delivery_hours
    if delivery_hours >= expiry_hours:
        return "HIGH", delivery_hours
    if remaining <= 6:
        return "MEDIUM", delivery_hours
    return "LOW", delivery_hours

def score_ngo(listing, demand, distance_km):
    """Return a 0-100 score for how suitable an NGO is for a listing."""
    # Compatibility: whether NGO accepts the food type
    food_type = listing.get("food_type", "").lower()
    accepted = [ft.lower() for ft in demand.get("food_types", [])]
    compatibility = 1.0 if any(food_type in a for a in accepted) or any(a in food_type for a in accepted) else 0.0

    # Capacity: how much of the listing the NGO can accept (capped at 1)
    cap = demand.get("capacity_kg", 0) or 0
    capacity_score = min(cap / (listing.get("quantity", 1) + 1e-6), 1.0)

    # People served: larger organisations may be prioritized
    people = demand.get("people_served", 0)
    people_score = min(people / 500.0, 1.0)

    # Urgency: listings with low expiry_hours are higher priority
    expiry = listing.get("expiry_hours", 48)
    urgency = max(0.0, (48 - expiry) / 48.0)

    # Distance penalty: prefer nearer NGOs (closer gets higher score)
    distance_score = 1.0 - min(distance_km / 100.0, 1.0)

    # Weighted sum
    w = {
        'compat': 0.4,
        'capacity': 0.2,
        'people': 0.15,
        'urgency': 0.15,
        'distance': 0.1
    }
    score = (compatibility * w['compat'] + capacity_score * w['capacity'] + people_score * w['people'] + urgency * w['urgency'] + distance_score * w['distance']) * 100
    return round(score, 1)

def vehicle_capacity(vehicle):
    caps = {
        'bike': 50.0,
        'car': 300.0,
        'auto': 150.0,
        'van': 1200.0,
        'cycle': 20.0
    }
    return caps.get(vehicle.lower(), 200.0)

def score_driver(driver, listing, distance_km_to_pickup):
    """Return 0-100 score for a driver candidate."""
    avail = 1.0 if driver.get('available', False) else 0.0
    # Area match: simple substring match between driver's area and pickup/dropoff
    area = (driver.get('area') or '').lower()
    pickup = (listing.get('location') or '').lower()
    area_match = 1.0 if area and (area in pickup or pickup in area) else 0.0

    cap = vehicle_capacity(driver.get('vehicle', 'car'))
    capacity_score = min(cap / (listing.get('quantity', 1) + 1e-6), 1.0)

    # Distance penalty: closer drivers are preferred
    distance_score = 1.0 - min(distance_km_to_pickup / 100.0, 1.0)

    w = {'avail':0.35, 'area':0.25, 'cap':0.25, 'dist':0.15}
    score = (avail*w['avail'] + area_match*w['area'] + capacity_score*w['cap'] + distance_score*w['dist'])*100
    return round(score,1)

def load_impact():
    return load_json(IMPACT_FILE, {"meals_served":0,"kg_rescued":0.0,"co2_saved":0.0,"ngos_served":0})

def save_impact(impact):
    save_json(IMPACT_FILE, impact)

def update_impact(match):
    impact = load_impact()
    qty = float(match.get('quantity', 0) or 0)
    impact['kg_rescued'] = impact.get('kg_rescued', 0.0) + qty
    impact['meals_served'] = int(impact.get('meals_served', 0) + (qty / MEAL_KG))
    impact['co2_saved'] = impact.get('co2_saved', 0.0) + qty * CO2_PER_KG
    # increment unique ngos served count conservatively
    impact['ngos_served'] = impact.get('ngos_served', 0) + 1
    save_impact(impact)

# ─── Auth ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        users = load_json(USERS_FILE, {})
        email = request.form["email"].strip().lower()
        if email in users:
            flash("Email already registered.", "error")
            return redirect(url_for("register"))
        role = request.form["role"]
        user_id = str(uuid.uuid4())
        users[email] = {
            "id": user_id,
            "name": request.form["name"],
            "email": email,
            "password": hash_pw(request.form["password"]),
            "role": role,
            "org": request.form.get("org", ""),
            "location": request.form.get("location", ""),
            "joined": datetime.now().isoformat()
        }
        save_json(USERS_FILE, users)
        session["user_id"] = user_id
        session["user_email"] = email
        session["user_name"] = request.form["name"]
        session["user_role"] = role
        flash("Account created! You can start using HarvestHaul right away.", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        users = load_json(USERS_FILE, {})
        email = request.form["email"].strip().lower()
        user = users.get(email)
        if not user or user["password"] != hash_pw(request.form["password"]):
            flash("Invalid email or password.", "error")
            return redirect(url_for("dashboard"))
        session["user_id"] = user["id"]
        session["user_email"] = email
        session["user_name"] = user["name"]
        session["user_role"] = user["role"]
        return redirect(url_for("dashboard"))
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    ensure_user_session()
    return redirect(url_for("dashboard"))

# ─── Dashboard ───────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    ensure_user_session()
    role = session["user_role"]
    listings = load_json(LISTINGS_FILE)
    demands = load_json(DEMANDS_FILE)
    matches = load_json(MATCHES_FILE)
    uid = session["user_id"]

    my_listings = [l for l in listings if l.get("donor_id") == uid]
    my_demands = [d for d in demands if d.get("ngo_id") == uid]
    my_matches = [m for m in matches if m.get("donor_id") == uid or m.get("ngo_id") == uid]

    stats = {
        "total_listings": len(listings),
        "active_matches": len([m for m in matches if m["status"] == "active"]),
        "kg_rescued": sum(l.get("quantity", 0) for l in listings if l.get("status") == "matched"),
        "ngos_served": len(set(m["ngo_id"] for m in matches))
    }

    impact = load_impact()

    return render_template("dashboard.html",
        role=role,
        user_name=session["user_name"],
        listings=listings, demands=demands, matches=matches,
        my_listings=my_listings, my_demands=my_demands, my_matches=my_matches,
        stats=stats,
        impact=impact,
        llm_available=gemini_client is not None
    )

# ─── Supplier Agent ──────────────────────────────────────────────────────────

@app.route("/add-listing", methods=["GET", "POST"])
@login_required
def add_listing():
    ensure_user_session()
    if request.method == "POST":
        listings = load_json(LISTINGS_FILE)
        listing = {
            "id": str(uuid.uuid4()),
            "donor_id": session["user_id"],
            "donor_name": session["user_name"],
            "food_type": request.form["food_type"],
            "quantity": float(request.form["quantity"]),
            "unit": request.form["unit"],
            "expiry_hours": int(request.form["expiry_hours"]),
            "location": request.form["location"],
            "notes": request.form.get("notes", ""),
            "status": "available",
            "created": datetime.now().isoformat()
        }
        listings.append(listing)
        save_json(LISTINGS_FILE, listings)
        run_matchmaker()
        flash("Listing added! Our agent is finding a match now.", "success")
        return redirect(url_for("dashboard"))
    return render_template("add_listing.html")

# ─── Demand Agent ────────────────────────────────────────────────────────────

@app.route("/add-demand", methods=["GET", "POST"])
@login_required
def add_demand():
    ensure_user_session()
    if request.method == "POST":
        demands = load_json(DEMANDS_FILE)
        demand = {
            "id": str(uuid.uuid4()),
            "ngo_id": session["user_id"],
            "ngo_name": session["user_name"],
            "food_types": request.form.getlist("food_types"),
            "capacity_kg": float(request.form["capacity_kg"]),
            "location": request.form["location"],
            "people_served": int(request.form.get("people_served", 0)),
            "notes": request.form.get("notes", ""),
            "status": "open",
            "created": datetime.now().isoformat()
        }
        demands.append(demand)
        save_json(DEMANDS_FILE, demands)
        run_matchmaker()
        flash("Need registered! Our agent will find suitable donors.", "success")
        return redirect(url_for("dashboard"))
    return render_template("add_demand.html")

# ─── Logistics Agent (Matchmaker) ────────────────────────────────────────────

def run_matchmaker():
    listings = load_json(LISTINGS_FILE)
    demands = load_json(DEMANDS_FILE)
    matches = load_json(MATCHES_FILE)
    drivers = load_json(DRIVERS_FILE)
    existing_listing_ids = {m["listing_id"] for m in matches if m.get("status") == "active"}

    for listing in listings:
        if listing.get("status") != "available":
            continue
        if listing.get("id") in existing_listing_ids:
            continue
        if listing.get("expiry_hours", 0) <= 0:
            listing["status"] = "expired"
            continue

        # Score all open demands and pick the best one
        best_demand = None
        best_score = -1
        best_distance = None
        for demand in demands:
            if demand.get("status") != "open":
                continue
            dist_km = calc_distance_km(listing.get("location"), demand.get("location"))
            ngo_score = score_ngo(listing, demand, dist_km)
            if ngo_score > best_score:
                best_score = ngo_score
                best_demand = demand
                best_distance = dist_km

        if not best_demand:
            continue

        # Pick best driver by scoring
        best_driver = None
        best_driver_score = -1
        driver_distance = None
        for driver in drivers:
            # estimate distance from driver area to pickup
            dist_to_pickup = calc_distance_km(driver.get('area'), listing.get('location'))
            dscore = score_driver(driver, listing, dist_to_pickup)
            if dscore > best_driver_score:
                best_driver_score = dscore
                best_driver = driver
                driver_distance = dist_to_pickup

        # build match object with scoring metadata
        driver_name = best_driver.get('name') if best_driver else (drivers[0]['name'] if drivers else "Volunteer Needed")
        driver_score_val = best_driver_score if best_driver else 0
        ngo_score_val = best_score if best_demand else 0
        distance_km = best_distance or 0.0
        spoilage_level, delivery_hours = calc_spoilage_risk(listing.get('expiry_hours', 0), distance_km)

        match = {
            "id": str(uuid.uuid4()),
            "listing_id": listing["id"],
            "donor_id": listing["donor_id"],
            "donor_name": listing["donor_name"],
            "ngo_id": best_demand.get("ngo_id"),
            "ngo_name": best_demand.get("ngo_name"),
            "food_type": listing["food_type"],
            "quantity": listing["quantity"],
            "unit": listing["unit"],
            "pickup_location": listing["location"],
            "dropoff_location": best_demand.get("location"),
            "expiry_hours": listing["expiry_hours"],
            "status": "active",
            "driver": driver_name,
            "driver_score": driver_score_val,
            "ngo_score": ngo_score_val,
            "distance_km": round(distance_km,1),
            "spoilage_risk": spoilage_level,
            "delivery_hours": round(delivery_hours,2),
            "created": datetime.now().isoformat(),
            "eta": (datetime.now() + timedelta(hours=delivery_hours)).isoformat()
        }

        matches.append(match)
        listing["status"] = "matched"
        try:
            send_notification(match)
        except Exception as e:
            print("Failed to send notification:", e)

        try:
            update_impact(match)
        except Exception as e:
            print("Impact update failed:", e)

    save_json(LISTINGS_FILE, listings)
    save_json(MATCHES_FILE, matches)

@app.route("/api/run-agent", methods=["POST"])
@login_required
def run_agent():
    run_matchmaker()
    return jsonify({"status": "ok", "message": "Logistics agent executed"})

@app.route("/api/stats")
def api_stats():
    listings = load_json(LISTINGS_FILE)
    matches = load_json(MATCHES_FILE)
    impact = load_impact()
    return jsonify({
        "total_listings": len(listings),
        "matched": len([l for l in listings if l.get("status") == "matched"]),
        "active_rescues": len([m for m in matches if m.get("status") == "active"]),
        "kg_rescued": sum(l.get("quantity", 0) for l in listings if l.get("status") == "matched"),
        "impact": impact
    })


# ─── LLM Chat Endpoint ──────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    """Process a chat message through Gemini LLM with MCP tool calling."""
    if not gemini_client:
        return jsonify({
            "success": False,
            "reply": "⚠️ AI assistant is not configured. Please add your GEMINI_API_KEY to the .env file and restart the server."
        }), 503

    data = request.get_json()
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"success": False, "reply": "Please type a message."}), 400

    # Load chat history for context (last 10 messages)
    chat_history = load_json(CHAT_HISTORY_FILE, [])
    session_id = session.get("user_id", "guest-user")
    session_history = [h for h in chat_history if h.get("session_id") == session_id][-10:]

    try:
        from google.genai import types
        from mcp_server import execute_tool

        # Build conversation history for Gemini
        contents = []
        for h in session_history:
            contents.append(types.Content(
                role="user",
                parts=[types.Part.from_text(text=h["user_message"])]
            ))
            contents.append(types.Content(
                role="model",
                parts=[types.Part.from_text(text=h["ai_reply"])]
            ))
        
        # Add current user message
        contents.append(types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_message)]
        ))

        # Build tools
        tools = _build_gemini_tools()

        # Call Gemini
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[tools],
                temperature=0.7,
            )
        )

        # Handle tool calls (function calling loop)
        max_iterations = 5
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            
            # Check if there are function calls in the response
            has_function_call = False
            if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if part.function_call:
                        has_function_call = True
                        break
            
            if not has_function_call:
                break
            
            # Add model's response to contents
            contents.append(response.candidates[0].content)
            
            # Execute all function calls and collect results
            function_response_parts = []
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    tool_name = part.function_call.name
                    tool_args = dict(part.function_call.args) if part.function_call.args else {}
                    
                    print(f"[LLM Tool Call] {tool_name}({tool_args})")
                    
                    # Execute via MCP server
                    result = execute_tool(
                        tool_name, 
                        tool_args,
                        user_id=session.get("user_id", "guest-user"),
                        user_name=session.get("user_name", "Guest User")
                    )
                    
                    print(f"[LLM Tool Result] {tool_name} -> success={result.get('success')}")
                    
                    function_response_parts.append(
                        types.Part.from_function_response(
                            name=tool_name,
                            response=result
                        )
                    )
            
            # Add function responses to contents
            contents.append(types.Content(
                role="user",
                parts=function_response_parts
            ))
            
            # Call Gemini again with function results
            response = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=[tools],
                    temperature=0.7,
                )
            )

        # Extract final text response
        reply = ""
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.text:
                    reply += part.text
        
        if not reply:
            reply = "I processed your request but didn't have anything additional to say. Check the dashboard for updates!"

        # Save to chat history
        chat_entry = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "user_message": user_message,
            "ai_reply": reply,
            "timestamp": datetime.now().isoformat()
        }
        chat_history.append(chat_entry)
        # Keep only last 100 entries
        if len(chat_history) > 100:
            chat_history = chat_history[-100:]
        save_json(CHAT_HISTORY_FILE, chat_history)

        return jsonify({"success": True, "reply": reply})

    except Exception as e:
        print(f"[LLM Error] {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "reply": f"Sorry, I encountered an error processing your request. Please try again. ({type(e).__name__})"
        }), 500


@app.route("/api/chat/history")
@login_required
def api_chat_history():
    """Get recent chat history for the current session."""
    chat_history = load_json(CHAT_HISTORY_FILE, [])
    session_id = session.get("user_id", "guest-user")
    session_history = [h for h in chat_history if h.get("session_id") == session_id][-20:]
    return jsonify({"success": True, "history": session_history})


@app.route('/seed')
def seed():
    os.makedirs(DATA_DIR, exist_ok=True)
    # Users
    users = {
        'donor@demo.local': {
            'id': str(uuid.uuid4()), 'name': 'Baker\'s Farm', 'email': 'donor@demo.local',
            'password': hash_pw('password123'), 'role': 'donor', 'org': "Baker's Farm", 'location': 'Local Town', 'joined': datetime.now().isoformat()
        },
        'ngo@demo.local': {
            'id': str(uuid.uuid4()), 'name': 'Downtown Shelter', 'email': 'ngo@demo.local',
            'password': hash_pw('password123'), 'role': 'ngo', 'org': 'Downtown Shelter', 'location': 'City Center', 'joined': datetime.now().isoformat()
        },
        'driver@demo.local': {
            'id': str(uuid.uuid4()), 'name': 'Alex Driver', 'email': 'driver@demo.local',
            'password': hash_pw('password123'), 'role': 'driver', 'org': '', 'location': 'City Center', 'joined': datetime.now().isoformat()
        }
    }
    save_json(USERS_FILE, users)

    drivers = [{
        'id': str(uuid.uuid4()), 'user_id': users['driver@demo.local']['id'], 'name': 'Alex Driver',
        'phone': '+0000000000', 'vehicle': 'Car', 'area': 'City Center', 'available': True, 'registered': datetime.now().isoformat()
    }]
    save_json(DRIVERS_FILE, drivers)

    listings = [{
        'id': str(uuid.uuid4()), 'donor_id': users['donor@demo.local']['id'], 'donor_name': users['donor@demo.local']['name'],
        'food_type': 'Tomatoes', 'quantity': 50.0, 'unit': 'kg', 'expiry_hours': 48, 'location': 'Baker\'s Farm', 'notes': '', 'status': 'available', 'created': datetime.now().isoformat()
    }]
    save_json(LISTINGS_FILE, listings)

    demands = [{
        'id': str(uuid.uuid4()), 'ngo_id': users['ngo@demo.local']['id'], 'ngo_name': users['ngo@demo.local']['name'],
        'food_types': ['Tomatoes'], 'capacity_kg': 200.0, 'location': 'Downtown Shelter', 'people_served': 120, 'notes': '', 'status': 'open', 'created': datetime.now().isoformat()
    }]
    save_json(DEMANDS_FILE, demands)

    run_matchmaker()
    return jsonify({'status': 'ok', 'message': 'Seeded demo data'})

# ─── Volunteer Driver Registration ───────────────────────────────────────────

@app.route("/register-driver", methods=["GET", "POST"])
@login_required
def register_driver():
    ensure_user_session()
    if request.method == "POST":
        drivers = load_json(DRIVERS_FILE)
        driver = {
            "id": str(uuid.uuid4()),
            "user_id": session["user_id"],
            "name": session["user_name"],
            "phone": request.form["phone"],
            "vehicle": request.form["vehicle"],
            "area": request.form["area"],
            "available": True,
            "registered": datetime.now().isoformat()
        }
        drivers.append(driver)
        save_json(DRIVERS_FILE, drivers)
        flash("You're registered as a volunteer driver!", "success")
        return redirect(url_for("dashboard"))
    return render_template("register_driver.html")

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    app.run(debug=True, port=5000)
