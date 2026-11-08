#pragma once
#include <cstddef>

#include "fdv_native.h"

namespace fdv {
int run_batch(int op, fdv_job* jobs, size_t njobs, int threads, int stop_on_error, fdv_progress_fn cb, void* user);
}
