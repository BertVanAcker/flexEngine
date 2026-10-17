// Minimal host shim of Arduino's String (only what otaCommand.cpp uses), for a PC check of the proposed patch.
#pragma once
#include <cstdint>
#include <cstring>
#include <string>
typedef uint8_t byte;

class String {
 public:
  String() {}
  String(const char* s) : s_(s ? s : "") {}
  String(const std::string& s) : s_(s) {}
  explicit String(long v) : s_(std::to_string(v)) {}
  explicit String(unsigned long v) : s_(std::to_string(v)) {}
  explicit String(int v) : s_(std::to_string(v)) {}
  unsigned int length() const { return s_.size(); }
  const char* c_str() const { return s_.c_str(); }
  char operator[](unsigned int i) const { return s_[i]; }
  bool operator==(const String& o) const { return s_ == o.s_; }
  bool operator==(const char* o) const { return s_ == o; }
  friend String operator+(const String& a, const String& b) { return String(a.s_ + b.s_); }
  friend String operator+(const String& a, const char* b) { return String(a.s_ + b); }
 private:
  std::string s_;
};
