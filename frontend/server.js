const express = require("express");
const path = require("path");
const fetch = require("node-fetch");

const app = express();
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8080";

app.use(express.static(__dirname));
app.use(express.json());

const METHODS_WITH_BODY = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function proxyToBackend(req, res) {
  try {
    const url = `${BACKEND_URL}${req.originalUrl}`;
    const response = await fetch(url, {
      method: req.method,
      headers: { "Content-Type": "application/json" },
      body: METHODS_WITH_BODY.has(req.method) ? JSON.stringify(req.body) : undefined,
    });

    const text = await response.text();
    res.status(response.status);
    if (response.headers.get("content-type")?.includes("application/json")) {
      res.set("Content-Type", "application/json");
      res.send(text);
    } else {
      res.send(text);
    }
  } catch (err) {
    console.error("Error calling backend:", err);
    res.status(500).json({ error: "Failed to connect to backend" });
  }
}

app.post("/chat", proxyToBackend);
app.all("/chats", proxyToBackend);
app.all("/chats/:id", proxyToBackend);
app.all("/stats", proxyToBackend);

app.get("/chats", async (req, res) => {
  try {
    const response = await fetch(`${BACKEND_URL}/chats`);
    const data = await response.json();
    res.json(data);
  } catch (err) {
    console.error("Error fetching chats:", err);
    res.status(500).json({ error: "Failed to fetch chats" });
  }
});

app.get("/chats/:id", async (req, res) => {
  try {
    const response = await fetch(`${BACKEND_URL}/chats/${req.params.id}`);
    const data = await response.json();
    res.json(data);
  } catch (err) {
    console.error("Error fetching chat:", err);
    res.status(500).json({ error: "Failed to fetch chat" });
  }
});

app.delete("/chats/:id", async (req, res) => {
  try {
    const response = await fetch(`${BACKEND_URL}/chats/${req.params.id}`, {
      method: "DELETE",
    });
    const data = await response.json();
    res.json(data);
  } catch (err) {
    console.error("Error deleting chat:", err);
    res.status(500).json({ error: "Failed to delete chat" });
  }
});

app.delete("/chats", async (req, res) => {
  try {
    const response = await fetch(`${BACKEND_URL}/chats`, {
      method: "DELETE",
    });
    const data = await response.json();
    res.json(data);
  } catch (err) {
    console.error("Error clearing chats:", err);
    res.status(500).json({ error: "Failed to clear chats" });
  }
});

app.get("/health", (req, res) => {
  res.json({ status: "ok" });
});

app.listen(8080, () => {
  console.log(" 🤖 NecipGPT running at http://localhost:8080");
  console.log(`Using backend: ${BACKEND_URL}`);
});
