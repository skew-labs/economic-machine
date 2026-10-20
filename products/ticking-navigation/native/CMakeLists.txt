cmake_minimum_required(VERSION 3.20)
project(economic_ticking_navigation LANGUAGES CXX)
add_library(ticking_navigation SHARED router.cpp)
target_compile_features(ticking_navigation PRIVATE cxx_std_20)
target_compile_options(ticking_navigation PRIVATE -Wall -Wextra -Werror -Wno-misleading-indentation -fstack-protector-strong)
target_link_options(ticking_navigation PRIVATE -Wl,-z,relro,-z,now)
# commands.generated.hpp is produced from the checked product catalog at release time.
