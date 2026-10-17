// PC check of the proposed flexNode-arduino src/commands/otaCommand.cpp: verifies commands signed by the
// flexEngineV2 arduino-ota driver.   main <key-hex> <command.json> [<file to hash>]
#include <cstdio>
#include <fstream>
#include <sstream>
#include <vector>
#include "commands/otaCommand.h"

int main(int argc, char** argv) {
  std::vector<uint8_t> key;
  for (const char* p = argv[1]; p[0] && p[1]; p += 2) {
    unsigned v;
    sscanf(p, "%2x", &v);
    key.push_back(static_cast<uint8_t>(v));
  }
  std::ifstream f(argv[2]);
  std::stringstream ss;
  ss << f.rdbuf();
  std::string js = ss.str();
  OtaCommand c;
  OtaCommandResult r = parse_ota_command(reinterpret_cast<const uint8_t*>(js.data()), js.size(), key.data(),
                                         key.size(), c);
  printf("result=%s", ota_command_result_text(r));
  if (r == OtaCommandResult::OK) printf(" version=%s size=%lu url=%s", c.version.c_str(), c.size, c.url.c_str());
  printf("\n");
  // the same command again: executed once only
  OtaCommand c2;
  printf("again=%s\n", ota_command_result_text(parse_ota_command(reinterpret_cast<const uint8_t*>(js.data()),
                                                                 js.size(), key.data(), key.size(), c2)));
  if (argc > 3) {
    char hex[65];
    printf("sha256=%s\n", sha256_file_hex(argv[3], hex) ? hex : "unreadable");
  }
  return 0;
}
