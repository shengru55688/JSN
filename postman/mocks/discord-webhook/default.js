const http = require("http");

http.createServer(async (req, res) => {
  if (req.method !== "POST" || !/^\/api\/webhooks\/(success|failure)\/token$/.test(req.url)) {
    res.writeHead(501);
    return res.end();
  }
  if (req.url.includes("/failure/")) {
    res.writeHead(500);
    return res.end();
  }
  let body = req.body;
  if (typeof body === "string") {
    try { body = JSON.parse(body); } catch { body = null; }
  }
  if (!body || typeof body.content !== "string" || !body.content.includes("/actions/runs/") ||
      !Array.isArray(body.allowed_mentions?.parse) || body.allowed_mentions.parse.length !== 0) {
    res.writeHead(400);
    return res.end();
  }
  res.writeHead(204);
  return res.end();
}).listen(process.env.PORT);
