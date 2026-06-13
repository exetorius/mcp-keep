// mcp-auth-relay — C++ implementation
// Lightweight MCP relay that injects a bearer token into every upstream request.
//
// Build: cmake -B build -S . && cmake --build build --config Release
// Run:   ./mcp-auth-relay  (reads config.json from the same directory as the executable)

#include <httplib.h>
#include <nlohmann/json.hpp>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>

namespace fs = std::filesystem;
using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

struct Config
{
    std::string bearer_token;
    std::string upstream_host  = "127.0.0.1";
    int         upstream_port  = 8088;
    int         proxy_port     = 8089;
    std::string manifest_path;
    std::string integration;
    std::string server_name    = "mcp-auth-relay";
    std::string instructions;
};

static Config load_config(const fs::path& path)
{
    Config cfg;
    if (!fs::exists(path)) return cfg;
    try
    {
        std::ifstream f(path);
        auto j = json::parse(f);
        cfg.bearer_token   = j.value("bearer_token",   "");
        cfg.upstream_host  = j.value("upstream_host",  "127.0.0.1");
        cfg.upstream_port  = j.value("upstream_port",  8088);
        cfg.proxy_port     = j.value("proxy_port",     8089);
        cfg.manifest_path  = j.value("manifest_path",  "");
        cfg.integration    = j.value("integration",    "");
        cfg.server_name    = j.value("server_name",    "mcp-auth-relay");
        cfg.instructions   = j.value("instructions",   "");
    }
    catch (...) {}
    return cfg;
}

// ---------------------------------------------------------------------------
// Manifest
// ---------------------------------------------------------------------------

static json load_manifest(const std::string& manifest_path)
{
    if (manifest_path.empty()) return json::array();
    try
    {
        std::ifstream f(manifest_path);
        if (!f.is_open()) return json::array();
        return json::parse(f);
    }
    catch (...) { return json::array(); }
}

// ---------------------------------------------------------------------------
// Integration pack
// ---------------------------------------------------------------------------
// An integration pack lives at integrations/<name>/ relative to the executable and supplies:
//   hints.json           — {"tool_name": "hint text"} appended to tool descriptions
//   synthetic_tools.json — extra tools served by the proxy (not forwarded upstream)
//   instructions.md      — agent instructions injected into initialize serverInfo

static json          g_hints;
static json          g_synthetic_tools = json::array();
static std::string   g_instructions;

static void load_integration(const fs::path& base_dir, const std::string& name, Config& cfg)
{
    if (name.empty()) return;

    fs::path pack = base_dir / "integrations" / name;
    if (!fs::exists(pack))
    {
        std::cout << "[relay] Integration pack '" << name << "' not found at " << pack << " — skipping.\n";
        return;
    }

    fs::path hints_path = pack / "hints.json";
    if (fs::exists(hints_path))
    {
        try
        {
            std::ifstream f(hints_path);
            g_hints = json::parse(f);
            std::cout << "[relay] Integration '" << name << "': loaded " << g_hints.size() << " hints.\n";
        }
        catch (const std::exception& e) { std::cout << "[relay] hints.json parse error: " << e.what() << "\n"; }
    }

    fs::path synth_path = pack / "synthetic_tools.json";
    if (fs::exists(synth_path))
    {
        try
        {
            std::ifstream f(synth_path);
            g_synthetic_tools = json::parse(f);
            std::cout << "[relay] Integration '" << name << "': loaded " << g_synthetic_tools.size() << " synthetic tools.\n";
        }
        catch (const std::exception& e) { std::cout << "[relay] synthetic_tools.json parse error: " << e.what() << "\n"; }
    }

    fs::path instr_path = pack / "instructions.md";
    if (fs::exists(instr_path) && cfg.instructions.empty())
    {
        try
        {
            std::ifstream f(instr_path);
            std::ostringstream ss;
            ss << f.rdbuf();
            cfg.instructions = ss.str();
            std::cout << "[relay] Integration '" << name << "': loaded instructions (" << cfg.instructions.size() << " bytes).\n";
        }
        catch (const std::exception& e) { std::cout << "[relay] instructions.md read error: " << e.what() << "\n"; }
    }
}

// ---------------------------------------------------------------------------
// Hint injection
// ---------------------------------------------------------------------------

static json apply_hints(const json& tools)
{
    if (g_hints.is_null() || g_hints.empty()) return tools;
    json result = json::array();
    for (auto tool : tools)
    {
        std::string name = tool.value("name", "");
        if (g_hints.contains(name))
        {
            std::string desc = tool.value("description", "") + g_hints[name].get<std::string>();
            tool["description"] = desc;
        }
        result.push_back(tool);
    }
    return result;
}

// ---------------------------------------------------------------------------
// Forward to upstream
// ---------------------------------------------------------------------------

struct ForwardResult { bool success; std::string body; };

static ForwardResult forward_to_upstream(const std::string& request_body, const Config& cfg)
{
    httplib::Client client(cfg.upstream_host, cfg.upstream_port);
    client.set_connection_timeout(2);
    client.set_read_timeout(120);

    httplib::Headers headers = {
        {"X-MCP-Auth-Relay", "true"},
        {"Connection",       "close"},
    };
    if (!cfg.bearer_token.empty())
        headers.emplace("Authorization", "Bearer " + cfg.bearer_token);

    for (int attempt = 0; attempt < 2; ++attempt)
    {
        auto r = client.Post("/mcp", headers, request_body, "application/json");
        if (r && r->status == 200) return {true, r->body};
        if (r && r->status != 200) return {false, r->body};
    }
    return {false, ""};
}

static json upstream_error_response(const json& req_id, const std::string& tool_name, const std::string& upstream_msg)
{
    std::string text;
    if (!upstream_msg.empty())
        text = "Upstream server rejected the request: " + upstream_msg + "\n"
               "Check that bearer_token in config.json matches the upstream server's expected token.";
    else
        text = "Upstream server is not running.\n"
               "Please start your MCP server, then retry '" + tool_name + "'.";

    return {
        {"jsonrpc", "2.0"},
        {"id", req_id},
        {"result", {
            {"content", json::array({{{"type", "text"}, {"text", text}}})},
            {"isError", true}
        }}
    };
}

// ---------------------------------------------------------------------------
// CORS
// ---------------------------------------------------------------------------

static void add_cors(httplib::Response& res)
{
    res.set_header("Access-Control-Allow-Origin",  "*");
    res.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS");
    res.set_header("Access-Control-Allow-Headers", "Content-Type, MCP-Protocol-Version, mcp-session-id");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main(int argc, char* argv[])
{
    fs::path exe_dir    = fs::path(argv[0]).parent_path();
    fs::path config_path = exe_dir / "config.json";

    Config cfg = load_config(config_path);
    load_integration(exe_dir, cfg.integration, cfg);

    if (!fs::exists(config_path))
        std::cout << "[relay] WARNING: config.json not found at " << config_path
                  << " — copy config.example.json and fill in your values.\n";
    else if (cfg.bearer_token.empty())
        std::cout << "[relay] WARNING: bearer_token is empty — upstream requests will be unauthenticated.\n";
    else
        std::cout << "[relay] Bearer token loaded.\n";

    if (!cfg.manifest_path.empty())
    {
        auto manifest = load_manifest(cfg.manifest_path);
        if (manifest.empty())
            std::cout << "[relay] Note: manifest not found at " << cfg.manifest_path
                      << " — tools/list will be empty until the upstream server writes it.\n";
        else
            std::cout << "[relay] Loaded " << manifest.size() << " tools from manifest.\n";
    }
    else
    {
        std::cout << "[relay] No manifest_path configured — tools/list served from upstream only.\n";
    }

    httplib::Server svr;

    svr.Options("/mcp", [](const httplib::Request&, httplib::Response& res) {
        add_cors(res);
        res.status = 200;
    });

    svr.Get("/mcp", [](const httplib::Request& req, httplib::Response& res) {
        if (req.get_header_value("Accept").find("text/event-stream") == std::string::npos)
        {
            add_cors(res);
            res.set_content("mcp-auth-relay running", "text/plain");
            return;
        }
        std::cout << "[relay] SSE stream opened\n";
        res.set_header("Cache-Control", "no-cache");
        res.set_header("Connection",    "keep-alive");
        add_cors(res);
        res.set_chunked_content_provider(
            "text/event-stream",
            [](size_t, httplib::DataSink& sink) {
                static const std::string hb = ": heartbeat\n\n";
                if (!sink.write(hb.c_str(), hb.size())) return false;
                std::this_thread::sleep_for(std::chrono::seconds(15));
                return true;
            },
            [](bool) { std::cout << "[relay] SSE stream closed\n"; }
        );
    });

    svr.Post("/mcp", [&cfg](const httplib::Request& req, httplib::Response& res) {
        add_cors(res);
        res.set_header("Content-Type", "application/json");

        json rpc;
        try { rpc = json::parse(req.body); }
        catch (...)
        {
            res.status = 400;
            res.set_content(R"({"error":"Invalid JSON"})", "application/json");
            return;
        }

        const auto method  = rpc.value("method", "");
        const auto req_id  = rpc.contains("id") ? rpc["id"] : json(nullptr);

        if (method == "initialize")
        {
            auto params         = rpc.value("params", json::object());
            auto client_version = params.value("protocolVersion", "2024-11-05");
            std::cout << "[relay] initialize (protocol " << client_version << ")\n";
            const std::string& instr = cfg.instructions.empty()
                ? (std::string("MCP relay active. Upstream: http://") + cfg.upstream_host + ":" +
                   std::to_string(cfg.upstream_port) + "/mcp. "
                   "Tools are forwarded to the upstream server with auth injected automatically.")
                : cfg.instructions;
            json response = {
                {"jsonrpc", "2.0"}, {"id", req_id},
                {"result", {
                    {"protocolVersion", client_version},
                    {"capabilities", {{"tools", json::object()}}},
                    {"serverInfo", {
                        {"name",         cfg.server_name},
                        {"version",      "1.0.0"},
                        {"instructions", instr}
                    }}
                }}
            };
            res.set_content(response.dump(), "application/json");
            return;
        }

        if (method == "notifications/initialized")
        {
            res.status = 202;
            return;
        }

        if (method == "tools/list")
        {
            auto tools = apply_hints(load_manifest(cfg.manifest_path));
            for (auto& t : g_synthetic_tools) tools.push_back(t);
            std::cout << "[relay] tools/list -> " << tools.size() << " tools\n";
            res.set_content(
                json({{"jsonrpc","2.0"},{"id",req_id},{"result",{{"tools",tools}}}}).dump(),
                "application/json"
            );
            return;
        }

        // Synthetic tool dispatch (integration packs can pre-populate g_synthetic_tools;
        // runtime handlers would go here — none built into core)

        // Forward everything else upstream
        auto [success, body] = forward_to_upstream(req.body, cfg);
        if (success)
        {
            std::cout << "[relay] " << method << " -> upstream\n";
            res.set_content(body, "application/json");
        }
        else
        {
            std::cout << "[relay] " << method << " -> upstream unreachable\n";
            if (method == "tools/call")
            {
                auto tool_name = rpc.value("/params/name"_json_pointer, std::string("unknown"));
                res.set_content(upstream_error_response(req_id, tool_name, body).dump(), "application/json");
            }
            else
            {
                res.set_content(
                    json({{"jsonrpc","2.0"},{"id",req_id},{"result",json::object()}}).dump(),
                    "application/json"
                );
            }
        }
    });

    std::cout << "[relay] mcp-auth-relay listening on http://127.0.0.1:" << cfg.proxy_port << "/mcp\n";
    std::cout << "[relay] Forwarding to upstream at http://" << cfg.upstream_host << ":" << cfg.upstream_port << "/mcp\n";

    svr.listen("127.0.0.1", cfg.proxy_port);
    return 0;
}
