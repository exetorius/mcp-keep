cmake_minimum_required(VERSION 3.14)
project(mcp-auth-relay CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

FetchContent_Declare(httplib
    GIT_REPOSITORY https://github.com/yhirose/cpp-httplib.git
    GIT_TAG        v0.18.7
    GIT_SHALLOW    TRUE
)
FetchContent_Declare(nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG        v3.11.3
    GIT_SHALLOW    TRUE
)

# Disable SSL and compression — relay only talks to localhost, no HTTPS needed.
# This eliminates libssl/libcrypto/zlib runtime dependencies.
set(HTTPLIB_USE_OPENSSL_IF_AVAILABLE OFF CACHE BOOL "" FORCE)
set(HTTPLIB_USE_ZLIB_IF_AVAILABLE    OFF CACHE BOOL "" FORCE)
set(HTTPLIB_USE_BROTLI_IF_AVAILABLE  OFF CACHE BOOL "" FORCE)

FetchContent_MakeAvailable(httplib nlohmann_json)

add_executable(mcp-auth-relay src/Main.cpp)

target_link_libraries(mcp-auth-relay
    PRIVATE httplib::httplib nlohmann_json::nlohmann_json
)

if(WIN32)
    target_link_libraries(mcp-auth-relay PRIVATE ws2_32)
endif()

# Static-link the MinGW/GCC runtime so the exe has no external DLL dependencies.
if(MINGW)
    target_link_options(mcp-auth-relay PRIVATE
        -static-libgcc
        -static-libstdc++
        -Wl,-Bstatic,--whole-archive -lwinpthread -Wl,--no-whole-archive,-Bdynamic
    )
endif()

# Output to bin/ next to the source
set(RELAY_BIN_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../bin")

set_target_properties(mcp-auth-relay PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY         "${RELAY_BIN_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG   "${RELAY_BIN_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE "${RELAY_BIN_DIR}"
)

message(STATUS "mcp-auth-relay output: ${RELAY_BIN_DIR}")
