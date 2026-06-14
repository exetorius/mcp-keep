// mcp-auth-relay — C++ implementation
// Lightweight MCP relay with bearer token injection, first-run setup,
// OS startup registration, /relay-* commands, and relay_install_pack MCP tool.
//
// Build: cmake -B build -S . && cmake --build build --config Release
// Run:   ./mcp-auth-relay  (reads config.json from the same directory as the executable)

#include <httplib.h>
#include <nlohmann/json.hpp>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>

#if defined(_WIN32)
#  include <windows.h>
#  include <io.h>
#  define IS_TTY (_isatty(_fileno(stdin)))
#  define POPEN  _popen
#  define PCLOSE _pclose
#else
#  include <unistd.h>
#  define IS_TTY (isatty(STDIN_FILENO))
#  define POPEN  popen
#  define PCLOSE pclose
#endif

namespace fs = std::filesystem;
using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

struct Config
{
    std::string bearer_token;
    std::string upstream_host     = "127.0.0.1";
    int         upstream_port     = 8088;
    int         proxy_port        = 8089;
    std::string manifest_path;
    std::string integration;
    std::string server_name       = "mcp-auth-relay";
    std::string instructions;
    bool        startup_asked     = false;
    bool        startup_registered = false;
};

static fs::path g_config_path;

static Config load_config(const fs::path& path)
{
    Config cfg;
    if (!fs::exists(path)) return cfg;
    try
    {
        std::ifstream f(path);
        auto j = json::parse(f);
        cfg.bearer_token        = j.value("bearer_token",        "");
        cfg.upstream_host       = j.value("upstream_host",       "127.0.0.1");
        cfg.upstream_port       = j.value("upstream_port",       8088);
        cfg.proxy_port          = j.value("proxy_port",          8089);
        cfg.integration         = j.value("integration",         "");
        cfg.server_name         = j.value("server_name",         "mcp-auth-relay");
        cfg.instructions        = j.value("instructions",        "");
        cfg.startup_asked       = j.value("startup_asked",       false);
        cfg.startup_registered  = j.value("startup_registered",  false);

        std::string raw_path = j.value("manifest_path", "");
#if defined(_WIN32)
        char expanded[MAX_PATH] = {};
        if (!raw_path.empty() && ExpandEnvironmentStringsA(raw_path.c_str(), expanded, MAX_PATH))
            cfg.manifest_path = expanded;
        else
            cfg.manifest_path = raw_path;
#else
        if (!raw_path.empty() && raw_path[0] == '~')
        {
            const char* home = std::getenv("HOME");
            cfg.manifest_path = std::string(home ? home : "") + raw_path.substr(1);
        }
        else cfg.manifest_path = raw_path;
#endif
    }
    catch (...) {}
    return cfg;
}

static void save_config_key(const std::string& key, const json& value)
{
    json j = json::object();
    if (fs::exists(g_config_path))
    {
        try { std::ifstream f(g_config_path); j = json::parse(f); }
        catch (...) {}
    }
    j[key] = value;
    std::ofstream f(g_config_path);
    f << j.dump(2);
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

static json        g_hints;
static json        g_synthetic_tools = json::array();
static std::string g_instructions;
static std::mutex  g_integration_mutex;

static std::string load_integration(const fs::path& repo_root, const std::string& name, Config& cfg)
{
    g_hints           = json::object();
    g_synthetic_tools = json::array();

    if (name.empty()) return "";

    fs::path pack = repo_root / "integrations" / name;
    if (!fs::exists(pack))
        return "Integration pack '" + name + "' not found at " + pack.string();

    std::vector<std::string> parts;

    fs::path hints_path = pack / "hints.json";
    if (fs::exists(hints_path))
    {
        try
        {
            std::ifstream f(hints_path);
            g_hints = json::parse(f);
            parts.push_back(std::to_string(g_hints.size()) + " hints");
        }
        catch (const std::exception& e) { parts.push_back(std::string("hints ERROR: ") + e.what()); }
    }

    fs::path synth_path = pack / "synthetic_tools.json";
    if (fs::exists(synth_path))
    {
        try
        {
            std::ifstream f(synth_path);
            g_synthetic_tools = json::parse(f);
            parts.push_back(std::to_string(g_synthetic_tools.size()) + " synthetic tools");
        }
        catch (const std::exception& e) { parts.push_back(std::string("synthetic_tools ERROR: ") + e.what()); }
    }

    fs::path instr_path = pack / "instructions.md";
    if (fs::exists(instr_path) && cfg.instructions.empty())
    {
        try
        {
            std::ifstream f(instr_path);
            std::ostringstream ss; ss << f.rdbuf();
            cfg.instructions = ss.str();
            g_instructions   = cfg.instructions;
            parts.push_back("instructions (" + std::to_string(cfg.instructions.size()) + " bytes)");
        }
        catch (const std::exception& e) { parts.push_back(std::string("instructions ERROR: ") + e.what()); }
    }

    std::string result = "Integration '" + name + "' loaded";
    if (!parts.empty())
    {
        result += " — ";
        for (size_t i = 0; i < parts.size(); ++i)
            result += (i ? ", " : "") + parts[i];
    }
    return result;
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
            tool["description"] = tool.value("description", "") + g_hints[name].get<std::string>();
        result.push_back(tool);
    }
    return result;
}

// ---------------------------------------------------------------------------
// OS startup registration
// ---------------------------------------------------------------------------

static fs::path g_exe_path;

static std::pair<bool, std::string> register_startup()
{
    std::string exe = g_exe_path.string();

#if defined(_WIN32)
    std::string cmd =
        "schtasks /Create /TN \"mcp-auth-relay\" /TR \"\\\"" + exe + "\\\"\" "
        "/SC ONLOGON /RL HIGHEST /F";
    int r = std::system(cmd.c_str());
    if (r == 0)
        return {true, "Registered via Task Scheduler — relay will start automatically at login."};
    return {false, "Task Scheduler registration failed. Try running as administrator."};

#elif defined(__APPLE__)
    fs::path plist_dir  = fs::path(std::getenv("HOME")) / "Library" / "LaunchAgents";
    fs::path plist_path = plist_dir / "com.mcp-auth-relay.plist";
    fs::create_directories(plist_dir);
    std::ofstream f(plist_path);
    f << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
      << "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
         "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
      << "<plist version=\"1.0\"><dict>\n"
      << "  <key>Label</key><string>com.mcp-auth-relay</string>\n"
      << "  <key>ProgramArguments</key><array><string>" << exe << "</string></array>\n"
      << "  <key>RunAtLoad</key><true/>\n"
      << "  <key>KeepAlive</key><true/>\n"
      << "</dict></plist>\n";
    f.close();
    std::system(("launchctl load " + plist_path.string()).c_str());
    return {true, "Registered via launchd — relay will start automatically at login."};

#else
    fs::path svc_dir  = fs::path(std::getenv("HOME")) / ".config" / "systemd" / "user";
    fs::path svc_path = svc_dir / "mcp-auth-relay.service";
    fs::create_directories(svc_dir);
    std::ofstream f(svc_path);
    f << "[Unit]\nDescription=mcp-auth-relay\nAfter=network.target\n\n"
      << "[Service]\nExecStart=" << exe << "\nRestart=on-failure\n\n"
      << "[Install]\nWantedBy=default.target\n";
    f.close();
    std::system("systemctl --user daemon-reload");
    int r = std::system("systemctl --user enable mcp-auth-relay");
    if (r == 0)
        return {true, "Registered via systemd user service — relay will start automatically at login."};
    return {false, "systemd registration failed."};
#endif
}

static std::pair<bool, std::string> unregister_startup()
{
#if defined(_WIN32)
    int r = std::system("schtasks /Delete /TN \"mcp-auth-relay\" /F");
    return r == 0
        ? std::make_pair(true,  std::string("Removed from Task Scheduler."))
        : std::make_pair(false, std::string("Could not remove — may not have been registered."));

#elif defined(__APPLE__)
    fs::path plist = fs::path(std::getenv("HOME")) / "Library" / "LaunchAgents" / "com.mcp-auth-relay.plist";
    std::system(("launchctl unload " + plist.string()).c_str());
    fs::remove(plist);
    return {true, "Removed from launchd."};

#else
    std::system("systemctl --user disable mcp-auth-relay");
    fs::remove(fs::path(std::getenv("HOME")) / ".config" / "systemd" / "user" / "mcp-auth-relay.service");
    return {true, "Removed from systemd."};
#endif
}

// ---------------------------------------------------------------------------
// Pack installer helpers (curl-based, works on Windows 10+, macOS, Linux)
// ---------------------------------------------------------------------------

static const std::string PACKS_REPO   = "exetorius/mcp-auth-relay-integrations";
static const std::string PACKS_BRANCH = "main";

static fs::path g_repo_root;

static std::string run_command_output(const std::string& cmd)
{
    std::string result;
    FILE* pipe = POPEN(cmd.c_str(), "r");
    if (!pipe) return "";
    char buf[512];
    while (fgets(buf, sizeof(buf), pipe))
        result += buf;
    PCLOSE(pipe);
    return result;
}

static std::string curl_get(const std::string& url)
{
    std::string cmd = "curl -s -L "
                      "-H \"User-Agent: mcp-auth-relay\" "
                      "-H \"Accept: application/vnd.github+json\" "
                      "\"" + url + "\"";
    return run_command_output(cmd);
}

static bool curl_download(const std::string& url, const fs::path& dest)
{
    fs::create_directories(dest.parent_path());
    std::string cmd = "curl -s -L "
                      "-H \"User-Agent: mcp-auth-relay\" "
                      "-o \"" + dest.string() + "\" "
                      "\"" + url + "\"";
    return std::system(cmd.c_str()) == 0;
}

// ---------------------------------------------------------------------------
// Post-install (non-interactive — auto-applies, returns summary string)
// ---------------------------------------------------------------------------

static std::string post_install_mcp_server_silent(const json& step)
{
    std::string server_name = step.value("server_name", "");
    auto server_config      = step.value("server_config", json::object());
    std::string target_str  = step.value("target", "~/.claude/.mcp.json");

    fs::path target;
#if defined(_WIN32)
    const char* home = std::getenv("USERPROFILE");
#else
    const char* home = std::getenv("HOME");
#endif
    if (!target_str.empty() && target_str[0] == '~')
        target = fs::path(home ? home : "") / target_str.substr(2);
    else
        target = target_str;

    json existing = json::object();
    if (fs::exists(target))
    {
        try { std::ifstream f(target); existing = json::parse(f); }
        catch (...) {}
    }
    if (!existing.contains("servers")) existing["servers"] = json::object();
    if (existing["servers"].contains(server_name))
        return "'" + server_name + "' is already configured in " + target.string() + ".";

    existing["servers"][server_name] = server_config;
    try
    {
        fs::create_directories(target.parent_path());
        std::ofstream f(target);
        f << existing.dump(2);
        return "Added '" + server_name + "' to " + target.string() +
               ". Restart Claude Code for the change to take effect.";
    }
    catch (const std::exception& e)
    {
        std::string snippet = json({{server_name, server_config}}).dump(4);
        return std::string("Could not write to ") + target.string() + ": " + e.what() +
               "\n\nAdd this manually to " + target.string() + " under \"servers\":\n\n  " + snippet;
    }
}

static std::string run_post_install_mcp(const fs::path& pack_dir)
{
    fs::path path = pack_dir / "post_install.json";
    if (!fs::exists(path)) return "";
    try
    {
        std::ifstream f(path);
        auto steps = json::parse(f);
        std::string result;
        for (auto& step : steps)
        {
            if (step.value("type", "") == "mcp_server")
            {
                auto r = post_install_mcp_server_silent(step);
                if (!r.empty()) result += (result.empty() ? "" : "\n") + r;
            }
        }
        return result;
    }
    catch (...) { return "Warning: could not process post_install.json"; }
}

// ---------------------------------------------------------------------------
// Conditional setup tools — disappear once relay is configured
// ---------------------------------------------------------------------------

static json get_setup_tools(const Config& cfg)
{
    json tools = json::array();
    if (cfg.integration.empty())
    {
        tools.push_back({
            {"name", "relay_install_pack"},
            {"description",
                "Install an integration pack for this MCP relay. "
                "Call with no arguments to list available packs, or with name='<pack>' to install one. "
                "Packs add tool hints, synthetic tools, and agent instructions tailored to your upstream MCP server. "
                "This tool disappears once a pack is installed."},
            {"inputSchema", {
                {"type", "object"},
                {"properties", {
                    {"name", {{"type","string"},{"description","Pack name to install. Omit to list available packs."}}}
                }},
                {"required", json::array()}
            }}
        });
    }
    return tools;
}

// ---------------------------------------------------------------------------
// relay_install_pack handler
// ---------------------------------------------------------------------------

static json make_tool_result(const json& req_id, const std::string& text, bool is_error = false)
{
    json result = {{"content", json::array({{{"type","text"},{"text",text}}})}};
    if (is_error) result["isError"] = true;
    return {{"jsonrpc","2.0"},{"id",req_id},{"result",result}};
}

static json handle_relay_install_pack(const json& req_id, const std::string& pack_name, Config& cfg)
{
    // No name — list available packs
    if (pack_name.empty())
    {
        std::string api_url = "https://api.github.com/repos/" + PACKS_REPO + "/contents";
        std::string raw = curl_get(api_url);
        try
        {
            auto arr = json::parse(raw);
            std::string text = "Available integration packs:\n";
            for (auto& entry : arr)
                if (entry.value("type","") == "dir")
                    text += "  - " + entry.value("name","") + "\n";
            text += "\nCall relay_install_pack with name='<pack>' to install one.";
            return make_tool_result(req_id, text);
        }
        catch (...) {
            return make_tool_result(req_id, "Could not reach GitHub to list packs. Check your internet connection.", true);
        }
    }

    // Download pack files
    std::string api_url = "https://api.github.com/repos/" + PACKS_REPO + "/contents/" + pack_name;
    std::string raw = curl_get(api_url);

    fs::path pack_dir = g_repo_root / "integrations" / pack_name;
    fs::create_directories(pack_dir);

    std::vector<std::string> downloaded;
    try
    {
        auto files = json::parse(raw);
        for (auto& f : files)
        {
            if (f.value("type","") != "file") continue;
            std::string fname = f.value("name","");
            std::string raw_url = "https://raw.githubusercontent.com/" + PACKS_REPO +
                                  "/" + PACKS_BRANCH + "/" + pack_name + "/" + fname;
            if (curl_download(raw_url, pack_dir / fname))
                downloaded.push_back(fname);
        }
    }
    catch (...) {
        return make_tool_result(req_id, "Failed to parse pack listing for '" + pack_name + "'. Check internet connection.", true);
    }

    if (downloaded.empty())
        return make_tool_result(req_id, "No files downloaded for pack '" + pack_name + "'.", true);

    // Save config and reload integration
    save_config_key("integration", pack_name);
    std::string status;
    {
        std::lock_guard<std::mutex> lock(g_integration_mutex);
        cfg.integration = pack_name;
        status = load_integration(g_repo_root, pack_name, cfg);
    }

    // Run post_install steps (non-interactive)
    std::string post_result = run_post_install_mcp(pack_dir);

    std::string text = "Downloaded " + std::to_string(downloaded.size()) + " files: ";
    for (size_t i = 0; i < downloaded.size(); ++i)
        text += (i ? ", " : "") + downloaded[i];
    text += "\n" + status;
    if (!post_result.empty()) text += "\n\n" + post_result;

    return make_tool_result(req_id, text);
}

// ---------------------------------------------------------------------------
// Setup menu
// ---------------------------------------------------------------------------

static Config* g_cfg_ptr = nullptr;

static void run_setup_menu()
{
    std::cout << "\n"
              << "  ╔══════════════════════════════════════════╗\n"
              << "  ║          mcp-auth-relay  setup           ║\n"
              << "  ╚══════════════════════════════════════════╝\n\n";

    if (g_cfg_ptr->startup_registered)
    {
        std::cout << "  Startup with OS: ENABLED\n\n"
                  << "  1. Disable startup with OS\n"
                  << "  2. Done\n\n"
                  << "  Enter choice [1-2]: " << std::flush;
        std::string choice; std::getline(std::cin, choice);
        if (choice == "1")
        {
            auto [ok, msg] = unregister_startup();
            std::cout << "\n  " << msg << "\n\n";
            save_config_key("startup_registered", false);
            save_config_key("startup_asked",      true);
            g_cfg_ptr->startup_registered = false;
        }
        return;
    }

    std::cout
        << "  How would you like to start the relay?\n\n"
        << "  1. Start with OS  (recommended)\n"
        << "     Relay starts automatically at login — no manual step needed.\n\n"
        << "  2. Start manually each time\n"
        << "     Run mcp-auth-relay when you need it.\n\n"
        << "  3. Ask me next time\n"
        << "     Start now, prompt again on next launch.\n\n"
        << "  Enter choice [1-3]: " << std::flush;

    std::string choice;
    std::getline(std::cin, choice);
    std::cout << "\n";

    if (choice == "1")
    {
        auto [ok, msg] = register_startup();
        std::cout << "  " << msg << "\n";
        if (!ok) std::cout << "  You may need to run as administrator and try again.\n";
        save_config_key("startup_asked",      true);
        save_config_key("startup_registered", ok);
        g_cfg_ptr->startup_registered = ok;
    }
    else if (choice == "2")
    {
        std::cout << "  Got it — starting manually. Type /relay-setup to change this later.\n";
        save_config_key("startup_asked",      true);
        save_config_key("startup_registered", false);
    }
    else
    {
        std::cout << "  OK — will ask again next time.\n";
        save_config_key("startup_asked",      false);
        save_config_key("startup_registered", false);
    }
    std::cout << "\n";
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

static void cmd_status(const Config& cfg)
{
    std::cout << "\n  mcp-auth-relay\n"
              << "  ----------------------------------------\n"
              << "  Listening:    http://127.0.0.1:" << cfg.proxy_port << "/mcp\n"
              << "  Upstream:     http://" << cfg.upstream_host << ":" << cfg.upstream_port << "/mcp\n"
              << "  Token:        " << (cfg.bearer_token.empty() ? "NOT SET" : "set") << "\n"
              << "  Manifest:     " << (cfg.manifest_path.empty() ? "not configured" : cfg.manifest_path) << "\n";
    {
        std::lock_guard<std::mutex> lock(g_integration_mutex);
        if (!cfg.manifest_path.empty())
            std::cout << "  Tools:        " << load_manifest(cfg.manifest_path).size()
                      << " from manifest + " << g_synthetic_tools.size() << " synthetic\n";
        if (!cfg.integration.empty())
            std::cout << "  Integration:  " << cfg.integration
                      << " (" << g_hints.size() << " hints, " << g_synthetic_tools.size() << " synthetic tools)\n";
        else
            std::cout << "  Integration:  none — type /relay-packs to install one, or ask the AI to run relay_install_pack\n";
    }
    std::cout << "  Startup:      " << (cfg.startup_registered ? "enabled" : "manual") << "\n\n";
}

static void cmd_reload(Config& cfg)
{
    cfg = load_config(g_config_path);
    std::string status;
    {
        std::lock_guard<std::mutex> lock(g_integration_mutex);
        status = load_integration(g_repo_root, cfg.integration, cfg);
    }
    std::cout << "  Reloaded. " << (status.empty() ? "No integration." : status) << "\n\n";
}

static void command_loop(Config& cfg)
{
    std::string line;
    while (std::getline(std::cin, line))
    {
        while (!line.empty() && (line.back() == '\r' || line.back() == '\n' || line.back() == ' '))
            line.pop_back();

        if (line.empty()) continue;

        if (line == "/relay-quit" || line == "/relay-exit")
        {
            std::cout << "[relay] Relay stopped.\n";
            std::exit(0);
        }
        else if (line == "/relay-setup")  { run_setup_menu(); }
        else if (line == "/relay-status") { cmd_status(cfg); }
        else if (line == "/relay-reload") { cmd_reload(cfg); }
        else if (line == "/relay-packs")
        {
            std::cout << "  /relay-packs: ask the AI to run relay_install_pack,\n"
                      << "  or use 'python proxy.py' for interactive pack installation.\n\n";
        }
        else
        {
            std::cout << "  Unknown command '" << line << "'.\n"
                      << "  Available: /relay-setup /relay-packs /relay-status /relay-reload /relay-quit\n\n";
        }
    }
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
    std::string text = upstream_msg.empty()
        ? "Upstream server is not running.\nPlease start your MCP server, then retry '" + tool_name + "'."
        : "Upstream server rejected the request: " + upstream_msg + "\n"
          "Check that bearer_token in config.json matches the upstream server's expected token.";
    return {{"jsonrpc","2.0"},{"id",req_id},
            {"result",{{"content",json::array({{{"type","text"},{"text",text}}})},{"isError",true}}}};
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
    g_exe_path   = fs::path(argv[0]).parent_path() / fs::path(argv[0]).filename();
    fs::path exe_dir  = fs::path(argv[0]).parent_path();
    g_repo_root  = exe_dir.parent_path(); // bin/ -> mcp-auth-relay/
    g_config_path = exe_dir / "config.json";

    bool first_run = !fs::exists(g_config_path);

    Config cfg = load_config(g_config_path);
    g_cfg_ptr  = &cfg;
    std::string intg_status;
    {
        std::lock_guard<std::mutex> lock(g_integration_mutex);
        intg_status = load_integration(g_repo_root, cfg.integration, cfg);
    }

    if (cfg.bearer_token.empty())
        std::cout << "[relay] WARNING: bearer_token is empty — upstream requests will be unauthenticated.\n";
    else
        std::cout << "[relay] Bearer token loaded.\n";

    if (!cfg.manifest_path.empty())
    {
        auto m = load_manifest(cfg.manifest_path);
        if (m.empty())
            std::cout << "[relay] Manifest not found at " << cfg.manifest_path << " — tools/list will be empty until upstream writes it.\n";
        else
            std::cout << "[relay] Manifest: " << m.size() << " tools loaded.\n";
    }

    if (!intg_status.empty()) std::cout << "[relay] " << intg_status << "\n";

    std::cout << "[relay] mcp-auth-relay started — listening on http://127.0.0.1:" << cfg.proxy_port << "/mcp\n";
    std::cout << "[relay] Forwarding to upstream at http://" << cfg.upstream_host << ":" << cfg.upstream_port << "/mcp\n";

    if (cfg.integration.empty())
        std::cout << "[relay] No integration pack — AI can run relay_install_pack to install one.\n";

    bool needs_setup = first_run || !cfg.startup_asked;
    if (needs_setup && IS_TTY)
        run_setup_menu();

    if (IS_TTY)
        std::thread([&cfg]() { command_loop(cfg); }).detach();

    httplib::Server svr;

    svr.Options("/mcp", [](const httplib::Request&, httplib::Response& res) {
        add_cors(res); res.status = 200;
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
        catch (...) { res.status = 400; res.set_content(R"({"error":"Invalid JSON"})", "application/json"); return; }

        const auto method = rpc.value("method", "");
        const auto req_id = rpc.contains("id") ? rpc["id"] : json(nullptr);

        if (method == "initialize")
        {
            auto client_version = rpc.value("params/protocolVersion"_json_pointer, std::string("2024-11-05"));
            std::cout << "[relay] initialize (protocol " << client_version << ")\n";
            std::string instr;
            {
                std::lock_guard<std::mutex> lock(g_integration_mutex);
                instr = cfg.instructions.empty()
                    ? std::string("MCP relay active. Upstream: http://") + cfg.upstream_host + ":"
                      + std::to_string(cfg.upstream_port) + "/mcp."
                    : cfg.instructions;
            }
            res.set_content(json({{"jsonrpc","2.0"},{"id",req_id},{"result",{
                {"protocolVersion", client_version},
                {"capabilities", {{"tools", json::object()}}},
                {"serverInfo", {{"name", cfg.server_name}, {"version","1.0.0"}, {"instructions", instr}}}
            }}}).dump(), "application/json");
            return;
        }

        if (method == "notifications/initialized") { res.status = 202; return; }

        if (method == "tools/list")
        {
            json tools;
            {
                std::lock_guard<std::mutex> lock(g_integration_mutex);
                tools = apply_hints(load_manifest(cfg.manifest_path));
                for (auto& t : g_synthetic_tools) tools.push_back(t);
            }
            for (auto& t : get_setup_tools(cfg)) tools.push_back(t);
            std::cout << "[relay] tools/list -> " << tools.size() << " tools\n";
            res.set_content(
                json({{"jsonrpc","2.0"},{"id",req_id},{"result",{{"tools",tools}}}}).dump(),
                "application/json");
            return;
        }

        if (method == "tools/call")
        {
            auto tool_name = rpc.value("/params/name"_json_pointer, std::string(""));
            if (tool_name == "relay_install_pack")
            {
                auto args      = rpc.value("/params/arguments"_json_pointer, json::object());
                auto pack_name = args.value("name", std::string(""));
                std::cout << "[relay] relay_install_pack('" << pack_name << "') -> handled by relay\n";
                res.set_content(handle_relay_install_pack(req_id, pack_name, cfg).dump(), "application/json");
                return;
            }
        }

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
                res.set_content(json({{"jsonrpc","2.0"},{"id",req_id},{"result",json::object()}}).dump(), "application/json");
        }
    });

    svr.listen("127.0.0.1", cfg.proxy_port);
    return 0;
}
