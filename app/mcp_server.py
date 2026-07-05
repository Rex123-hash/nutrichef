from mcp.server.fastmcp import FastMCP
import json
import os

# Create an MCP server
mcp = FastMCP("nutrichef_pantry")

# Mock database file
DB_FILE = os.path.join(os.path.dirname(__file__), "pantry_db.json")

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    # Default initial data
    return {
        "pantry": {
            "olive oil": "1 bottle",
            "salt": "1 pack",
            "pepper": "1 pack",
            "rice": "2 kg",
            "pasta": "1 kg",
            "canned tomatoes": "3 cans",
            "onions": "4",
            "garlic": "1 head"
        },
        "recipes": [
            {
                "name": "Simple Tomato Pasta",
                "ingredients": ["pasta", "canned tomatoes", "garlic", "olive oil", "salt"],
                "instructions": "Boil pasta. Sauté minced garlic in olive oil. Add tomatoes and simmer. Mix in pasta."
            },
            {
                "name": "Garlic Fried Rice",
                "ingredients": ["rice", "garlic", "olive oil", "salt", "pepper"],
                "instructions": "Sauté minced garlic in oil. Add cooked rice and stir-fry. Season with salt and pepper."
            },
            {
                "name": "Onion Tomato Stew",
                "ingredients": ["onions", "canned tomatoes", "garlic", "olive oil", "salt", "pepper"],
                "instructions": "Sauté sliced onions and minced garlic. Add canned tomatoes, cover and simmer. Season to taste."
            }
        ]
    }

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

@mcp.tool()
def get_pantry_items() -> str:
    """Get the list of ingredients currently in the pantry.
    
    Returns:
        A JSON string containing the pantry items and their quantities.
    """
    db = load_db()
    return json.dumps(db["pantry"], indent=2)

@mcp.tool()
def add_to_pantry(item: str, quantity: str) -> str:
    """Add or update an ingredient in the pantry.
    
    Args:
        item: The name of the ingredient (e.g. 'eggs', 'spinach').
        quantity: The quantity (e.g. '6 pcs', '200g').
        
    Returns:
        A confirmation message.
    """
    db = load_db()
    db["pantry"][item.lower()] = quantity
    save_db(db)
    return f"Successfully added/updated {item} with quantity {quantity} in the pantry."

@mcp.tool()
def search_recipes(query: str) -> str:
    """Search for recipes in the database by keyword.
    
    Args:
        query: Keyword to search (e.g. 'tomato', 'rice').
        
    Returns:
        A JSON string of matching recipes.
    """
    db = load_db()
    matches = []
    for r in db["recipes"]:
        if query.lower() in r["name"].lower() or any(query.lower() in ing.lower() for ing in r["ingredients"]):
            matches.append(r)
    return json.dumps(matches, indent=2)

if __name__ == "__main__":
    mcp.run()
