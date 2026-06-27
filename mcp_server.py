"""
MCP Tool Server for HarvestHaul
================================
Exposes HarvestHaul operations as callable tools for the LLM.
Each tool reads/writes the same JSON data files as app.py.
"""

import json, os, uuid
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

USERS_FILE   = os.path.join(DATA_DIR, "users.json")
LISTINGS_FILE = os.path.join(DATA_DIR, "listings.json")
DEMANDS_FILE  = os.path.join(DATA_DIR, "demands.json")
MATCHES_FILE  = os.path.join(DATA_DIR, "matches.json")
DRIVERS_FILE  = os.path.join(DATA_DIR, "drivers.json")
IMPACT_FILE   = os.path.join(DATA_DIR, "impact.json")

# ── Helpers ──────────────────────────────────────────────────────────────────

def _load(path, default=None):
    if default is None:
        default = []
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)

def _save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ── Tool definitions (for Gemini function-calling schema) ────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "create_listing",
        "description": "Create a new food surplus listing on the platform. Use this when a donor wants to donate/list surplus food. After creating, the matchmaker runs automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "food_type":    {"type": "string",  "description": "Type of food, e.g. Tomatoes, Bread, Rice, Dairy, Cooked Meals"},
                "quantity":     {"type": "number",  "description": "Amount of food (numeric value)"},
                "unit":         {"type": "string",  "enum": ["kg", "lbs", "litres", "portions", "boxes"], "description": "Unit of measurement"},
                "expiry_hours": {"type": "integer", "description": "Hours until the food expires (1-168)"},
                "location":     {"type": "string",  "description": "Pickup address or landmark"},
                "notes":        {"type": "string",  "description": "Optional notes about storage, contact info, etc."}
            },
            "required": ["food_type", "quantity", "unit", "expiry_hours", "location"]
        }
    },
    {
        "name": "create_demand",
        "description": "Register a food need/demand from an NGO, shelter, or food bank. Use when an organization needs food supplies.",
        "parameters": {
            "type": "object",
            "properties": {
                "location":      {"type": "string",  "description": "Address or area of the facility"},
                "capacity_kg":   {"type": "number",  "description": "How many kg the facility can accept"},
                "people_served": {"type": "integer", "description": "Number of people served daily"},
                "food_types":    {"type": "array",   "items": {"type": "string"}, "description": "List of food types accepted, e.g. ['Vegetables', 'Rice & Grains', 'Bread & Bakery']"},
                "notes":         {"type": "string",  "description": "Optional notes about dietary restrictions, operating hours, etc."}
            },
            "required": ["location", "capacity_kg", "food_types"]
        }
    },
    {
        "name": "run_matchmaker",
        "description": "Trigger the logistics matching agent to pair available food listings with open NGO demands and assign drivers. Run this to find new matches.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_dashboard_stats",
        "description": "Get an overview of platform statistics: total listings, active rescues, food rescued, NGOs served.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_active_rescues",
        "description": "Get all currently active food rescue matches (pickups in progress). Shows donor, NGO, driver, food type, route, and status.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_food_listings",
        "description": "Get food listings, optionally filtered by status. Use this to see what food is available, matched, or expired.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["available", "matched", "expired", "all"], "description": "Filter by status. Use 'all' or omit to get everything."}
            },
            "required": []
        }
    },
    {
        "name": "get_demands",
        "description": "Get NGO demands/needs, optionally filtered by status.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "fulfilled", "all"], "description": "Filter by status. Use 'all' or omit to get everything."}
            },
            "required": []
        }
    },
    {
        "name": "search_listings",
        "description": "Search food listings by food type keyword. Returns matching listings.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword, e.g. 'tomato', 'bread', 'rice'"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_impact_report",
        "description": "Get environmental and social impact metrics: kg of food rescued, meals served, CO2 saved, NGOs served.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_food_safety_advice",
        "description": "Get food safety, storage, and transport advice for a specific food type. Useful for donors and drivers.",
        "parameters": {
            "type": "object",
            "properties": {
                "food_type": {"type": "string", "description": "The food type to get advice for, e.g. 'dairy', 'meat', 'bread', 'vegetables'"}
            },
            "required": ["food_type"]
        }
    }
]


# ── Tool implementations ─────────────────────────────────────────────────────

def create_listing(food_type, quantity, unit, expiry_hours, location, notes="", donor_id="guest-user", donor_name="Guest User"):
    """Create a new food surplus listing and trigger matchmaker."""
    listings = _load(LISTINGS_FILE)
    listing = {
        "id": str(uuid.uuid4()),
        "donor_id": donor_id,
        "donor_name": donor_name,
        "food_type": food_type,
        "quantity": float(quantity),
        "unit": unit,
        "expiry_hours": int(expiry_hours),
        "location": location,
        "notes": notes or "",
        "status": "available",
        "created": datetime.now().isoformat()
    }
    listings.append(listing)
    _save(LISTINGS_FILE, listings)

    # Auto-run matchmaker
    from app import run_matchmaker
    run_matchmaker()

    return {
        "success": True,
        "message": f"✅ Created listing: {quantity} {unit} of {food_type} at {location} (expires in {expiry_hours}h). The matchmaker has been triggered automatically.",
        "listing_id": listing["id"]
    }


def create_demand(location, capacity_kg, food_types, people_served=0, notes="", ngo_id="guest-user", ngo_name="Guest User"):
    """Register an NGO food demand and trigger matchmaker."""
    demands = _load(DEMANDS_FILE)
    demand = {
        "id": str(uuid.uuid4()),
        "ngo_id": ngo_id,
        "ngo_name": ngo_name,
        "food_types": food_types if isinstance(food_types, list) else [food_types],
        "capacity_kg": float(capacity_kg),
        "location": location,
        "people_served": int(people_served) if people_served else 0,
        "notes": notes or "",
        "status": "open",
        "created": datetime.now().isoformat()
    }
    demands.append(demand)
    _save(DEMANDS_FILE, demands)

    # Auto-run matchmaker
    from app import run_matchmaker
    run_matchmaker()

    return {
        "success": True,
        "message": f"✅ Registered need: {capacity_kg} kg capacity for {', '.join(demand['food_types'])} at {location}. The matchmaker has been triggered.",
        "demand_id": demand["id"]
    }


def run_matchmaker_tool():
    """Trigger the logistics matching agent."""
    from app import run_matchmaker
    matches_before = len(_load(MATCHES_FILE))
    run_matchmaker()
    matches_after = len(_load(MATCHES_FILE))
    new_matches = matches_after - matches_before
    return {
        "success": True,
        "message": f"✅ Matchmaker completed. {new_matches} new match(es) created. Total active matches: {matches_after}.",
        "new_matches": new_matches,
        "total_matches": matches_after
    }


def get_dashboard_stats():
    """Get platform-wide statistics."""
    listings = _load(LISTINGS_FILE)
    matches = _load(MATCHES_FILE)
    impact = _load(IMPACT_FILE, {})
    return {
        "success": True,
        "total_listings": len(listings),
        "available_listings": len([l for l in listings if l.get("status") == "available"]),
        "matched_listings": len([l for l in listings if l.get("status") == "matched"]),
        "active_rescues": len([m for m in matches if m.get("status") == "active"]),
        "total_kg_rescued": sum(l.get("quantity", 0) for l in listings if l.get("status") == "matched"),
        "ngos_served": len(set(m.get("ngo_id", "") for m in matches)),
        "impact": impact
    }


def get_active_rescues():
    """Get all currently active rescue matches."""
    matches = _load(MATCHES_FILE)
    active = [m for m in matches if m.get("status") == "active"]
    if not active:
        return {"success": True, "message": "No active rescues right now.", "rescues": []}
    
    summaries = []
    for m in active:
        summaries.append({
            "food": f"{m.get('food_type')} — {m.get('quantity')} {m.get('unit')}",
            "route": f"{m.get('pickup_location')} → {m.get('dropoff_location')}",
            "donor": m.get("donor_name"),
            "ngo": m.get("ngo_name"),
            "driver": m.get("driver"),
            "distance_km": m.get("distance_km"),
            "spoilage_risk": m.get("spoilage_risk"),
            "eta": m.get("eta")
        })
    return {"success": True, "rescues": summaries, "count": len(summaries)}


def get_food_listings(status="all"):
    """Get food listings with optional status filter."""
    listings = _load(LISTINGS_FILE)
    if status and status != "all":
        listings = [l for l in listings if l.get("status") == status]
    
    summaries = []
    for l in listings:
        summaries.append({
            "food_type": l.get("food_type"),
            "quantity": f"{l.get('quantity')} {l.get('unit')}",
            "location": l.get("location"),
            "expiry_hours": l.get("expiry_hours"),
            "status": l.get("status"),
            "donor": l.get("donor_name"),
            "created": l.get("created")
        })
    return {"success": True, "listings": summaries, "count": len(summaries)}


def get_demands(status="all"):
    """Get NGO demands with optional status filter."""
    demands = _load(DEMANDS_FILE)
    if status and status != "all":
        demands = [d for d in demands if d.get("status") == status]
    
    summaries = []
    for d in demands:
        summaries.append({
            "ngo": d.get("ngo_name"),
            "location": d.get("location"),
            "capacity_kg": d.get("capacity_kg"),
            "food_types": d.get("food_types"),
            "people_served": d.get("people_served"),
            "status": d.get("status")
        })
    return {"success": True, "demands": summaries, "count": len(summaries)}


def search_listings(query):
    """Search food listings by keyword."""
    listings = _load(LISTINGS_FILE)
    query_lower = query.lower()
    matched = [l for l in listings if query_lower in l.get("food_type", "").lower() or query_lower in l.get("location", "").lower()]
    
    summaries = []
    for l in matched:
        summaries.append({
            "food_type": l.get("food_type"),
            "quantity": f"{l.get('quantity')} {l.get('unit')}",
            "location": l.get("location"),
            "status": l.get("status"),
            "expiry_hours": l.get("expiry_hours")
        })
    return {"success": True, "results": summaries, "count": len(summaries), "query": query}


def get_impact_report():
    """Get environmental and social impact metrics."""
    impact = _load(IMPACT_FILE, {})
    listings = _load(LISTINGS_FILE)
    matches = _load(MATCHES_FILE)
    
    total_rescued = sum(l.get("quantity", 0) for l in listings if l.get("status") == "matched")
    return {
        "success": True,
        "kg_rescued": impact.get("kg_rescued", total_rescued),
        "meals_served": impact.get("meals_served", 0),
        "co2_saved_kg": impact.get("co2_saved", 0),
        "ngos_served": impact.get("ngos_served", 0),
        "total_listings": len(listings),
        "total_matches": len(matches),
        "message": f"🌍 Impact so far: {impact.get('kg_rescued', 0):.1f} kg rescued, {impact.get('meals_served', 0)} meals served, {impact.get('co2_saved', 0):.1f} kg CO₂ saved."
    }


def get_food_safety_advice(food_type):
    """Return food safety and transport advice for a given food type."""
    advice_db = {
        "vegetables": {
            "storage": "Keep in a cool, ventilated area (2-8°C). Avoid direct sunlight. Use perforated bags for leafy greens.",
            "transport": "Use insulated containers. Keep separate from raw meat. Transport within 2 hours of pickup.",
            "shelf_life": "Most vegetables last 3-7 days when properly refrigerated.",
            "tips": "Sort and remove damaged items before transport. Wash only before consumption, not before storage."
        },
        "fruits": {
            "storage": "Store at room temperature if unripe, refrigerate once ripe. Keep ethylene-producing fruits (bananas, apples) separate.",
            "transport": "Use cushioned containers to prevent bruising. Keep away from heat sources.",
            "shelf_life": "Varies by type: berries 2-3 days, apples 2-4 weeks, bananas 3-5 days.",
            "tips": "Handle gently. Don't stack heavy items on top of delicate fruits."
        },
        "bread": {
            "storage": "Store at room temperature in breathable packaging. Freeze if not distributing within 2 days.",
            "transport": "Keep dry and avoid crushing. Use sturdy containers or bags.",
            "shelf_life": "2-4 days at room temperature, 2-3 months frozen.",
            "tips": "Wrap tightly to prevent staleness. Separate from strong-smelling foods."
        },
        "dairy": {
            "storage": "Must be refrigerated at 2-4°C at all times. Never break the cold chain.",
            "transport": "Use insulated coolers with ice packs. Monitor temperature throughout transport.",
            "shelf_life": "Milk: 5-7 days. Cheese: 1-4 weeks. Yogurt: 1-2 weeks.",
            "tips": "Check expiry dates carefully. Discard if left at room temperature for more than 2 hours."
        },
        "meat": {
            "storage": "Refrigerate at 0-4°C. Freeze at -18°C if not using within 2 days.",
            "transport": "CRITICAL: Maintain cold chain. Use insulated containers with ice packs. Keep separate from other foods.",
            "shelf_life": "Fresh: 1-2 days refrigerated. Frozen: 4-12 months.",
            "tips": "Never refreeze thawed meat. Place at the bottom of containers to prevent drip contamination."
        },
        "rice": {
            "storage": "Store in airtight containers in a cool, dry place. Cooked rice must be refrigerated and used within 1 day.",
            "transport": "Keep dry. Uncooked rice is stable for transport. Cooked rice needs cold chain.",
            "shelf_life": "Uncooked: 6-12 months. Cooked: 1 day refrigerated.",
            "tips": "Cooked rice is a high-risk food for bacterial growth. Refrigerate within 1 hour of cooking."
        },
        "cooked meals": {
            "storage": "Refrigerate within 2 hours of preparation. Keep at 4°C or below. Use airtight containers.",
            "transport": "Hot food: keep above 60°C. Cold food: keep below 4°C. Use insulated containers.",
            "shelf_life": "1-3 days refrigerated. Consume as soon as possible.",
            "tips": "Label with preparation time. Reheat to 75°C before serving. Don't mix old and fresh batches."
        },
        "eggs": {
            "storage": "Refrigerate at 4°C. Store in original carton. Keep away from strong-smelling foods.",
            "transport": "Handle very carefully. Use egg cartons or cushioned containers. Avoid temperature fluctuations.",
            "shelf_life": "3-5 weeks refrigerated from purchase date.",
            "tips": "Check for cracks before transport. Discard any cracked eggs. Don't wash before storage."
        },
        "canned goods": {
            "storage": "Store in a cool, dry place. Once opened, transfer to airtight containers and refrigerate.",
            "transport": "Sturdy and easy to transport. Check for dents, rust, or swelling — discard if found.",
            "shelf_life": "1-5 years unopened. 2-4 days after opening (refrigerated).",
            "tips": "Ideal for long-distance donations. Check best-before dates. Never use cans that are bulging."
        }
    }
    
    food_lower = food_type.lower().strip()
    # Try to find a match
    for key, advice in advice_db.items():
        if key in food_lower or food_lower in key:
            return {
                "success": True,
                "food_type": food_type,
                "advice": advice
            }
    
    # Generic advice
    return {
        "success": True,
        "food_type": food_type,
        "advice": {
            "storage": "Store in a clean, cool, dry area. Maintain appropriate temperature for the food type.",
            "transport": "Use clean, food-safe containers. Minimize transport time. Protect from contamination.",
            "shelf_life": "Check packaging for best-before dates. When in doubt, consult local food safety guidelines.",
            "tips": "Follow FIFO (First In, First Out). Label everything with date and contents. When in doubt, throw it out."
        }
    }


# ── Tool dispatcher ──────────────────────────────────────────────────────────

TOOL_MAP = {
    "create_listing":        create_listing,
    "create_demand":         create_demand,
    "run_matchmaker":        run_matchmaker_tool,
    "get_dashboard_stats":   get_dashboard_stats,
    "get_active_rescues":    get_active_rescues,
    "get_food_listings":     get_food_listings,
    "get_demands":           get_demands,
    "search_listings":       search_listings,
    "get_impact_report":     get_impact_report,
    "get_food_safety_advice": get_food_safety_advice,
}


def execute_tool(tool_name, arguments, user_id="guest-user", user_name="Guest User"):
    """Execute an MCP tool by name with given arguments. Injects user context for create operations."""
    func = TOOL_MAP.get(tool_name)
    if not func:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
    
    try:
        # Inject user context for create operations
        if tool_name == "create_listing":
            arguments["donor_id"] = user_id
            arguments["donor_name"] = user_name
        elif tool_name == "create_demand":
            arguments["ngo_id"] = user_id
            arguments["ngo_name"] = user_name
        
        result = func(**arguments)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}
