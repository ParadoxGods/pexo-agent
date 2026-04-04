import sqlite3
import json

def query_db():
    conn = sqlite3.connect('pexo.db')
    cursor = conn.cursor()
    
    print("--- RECENT CHAT MESSAGES ---")
    cursor.execute("SELECT role, content, created_at FROM chat_messages ORDER BY created_at DESC LIMIT 5")
    for row in cursor.fetchall():
        print(f"[{row[2]}] {row[0]}: {row[1][:200]}...")
        
    print("\n--- RECENT AGENT STATES ---")
    cursor.execute("SELECT agent_name, status, data, created_at FROM agent_states ORDER BY created_at DESC LIMIT 5")
    for row in cursor.fetchall():
        data_preview = row[2][:200] if row[2] else "N/A"
        print(f"[{row[3]}] Agent: {row[0]} | Status: {row[1]} | Data: {data_preview}...")
        
    conn.close()

if __name__ == "__main__":
    query_db()
