#include <pybind11/pybind11.h>

#include "perform_etl.hh"

PYBIND11_MODULE(_native, m) { 
    m.doc() = "C++ backend for meds_etl - provides optimized ETL algorithms";
    m.def("perform_etl", perform_etl, "Perform ETL on MEDS data");
}