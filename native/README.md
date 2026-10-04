# Native execution libraries

C++20 implements bounded arithmetic, typed economic programs, candidate search
and exact verification. Network clients, databases, model calls and wallet keys
remain outside these libraries.

| CMake target | Source and boundary |
| --- | --- |
| `machine_kernel` | [kernel](src/kernel.cpp), [program](src/program.cpp): typed state and program evaluation |
| `machine_economics` | [economic primitives](include/machine/economics/): fixed-point risk, allocation, routing and transitions |
| `machine_mining` | [mining.cpp](src/mining.cpp): bounded route search and score verification |
| `machine_solution` | [solution.cpp](src/solution.cpp): integer graph search and exact scoring |
| `machine_solution_pipeline` | [solution_pipeline.cpp](src/solution_pipeline.cpp): Linux worker stages, bounded SPSC queues and syscall sandbox |

```sh
cmake -S native -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 2
ctest --test-dir build/native --output-on-failure
```

The portable libraries require CMake 3.20+, a C++20 compiler and threads. The
sandboxed pipeline additionally requires Linux. Release conformance targets keep
assertions enabled. Python bindings require the exact library/executable hash;
an arbitrary shared library cannot be selected through a public API.

Outputs are candidates, not execution approval. Parent processes check bounds,
identity and score before creating an intent; signing and chain reconciliation
remain outside the worker. See [economic primitives](../docs/ECONOMIC_PRIMITIVES.md)
and [pipeline operation](../docs/SOLUTION_OPERATIONS.md).
