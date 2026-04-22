# Register a non-blocking lint-agent command for the ALINT-PRO Tcl console.
#
# Load once in ALINT-PRO console:
#   source D:/mcp/mcp_alint/langgraph_server/lint_agent_alint_console.tcl
#
# Then call:
#   lint-agent "prompt"
#   lint-agent -auto-approve "prompt"
#
# The command starts the agent in the background and returns the ALINT-PRO
# prompt immediately. Tcl polls the background process output file and prints
# the result back to the same ALINT-PRO console.

namespace eval ::LintAgent {
    variable python "D:/software/Miniconda3/envs/mcp/python.exe"
    variable cli "D:/mcp/mcp_alint/langgraph_server/lint_agent_cli.py"
    variable url "http://127.0.0.1:2024"
    variable job_counter 0
    variable jobs
}

set ::env(PYTHONUTF8) 1
set ::env(PYTHONIOENCODING) "utf-8"

proc ::LintAgent::parse_flags {arg_list} {
    set auto_approve 0
    set auto_reject 0
    set prompt_parts {}

    foreach arg $arg_list {
        if {$arg eq "-auto-approve" || $arg eq "--auto-approve"} {
            set auto_approve 1
        } elseif {$arg eq "-auto-reject" || $arg eq "--auto-reject"} {
            set auto_reject 1
        } else {
            lappend prompt_parts $arg
        }
    }

    set prompt [join $prompt_parts " "]
    if {[string trim $prompt] eq ""} {
        error {usage: lint-agent ?-auto-approve|-auto-reject? "prompt"}
    }
    if {$auto_approve && $auto_reject} {
        error "-auto-approve and -auto-reject cannot be used together"
    }

    return [list $auto_approve $auto_reject $prompt]
}

proc ::LintAgent::write_prompt_file {prompt} {
    variable job_counter
    incr job_counter
    set prompt_file [file normalize [file join [pwd] ".lint_agent_prompt_${job_counter}_[pid].txt"]]
    set f [open $prompt_file w]
    fconfigure $f -encoding utf-8
    puts -nonewline $f $prompt
    close $f
    return $prompt_file
}

proc ::LintAgent::job_dir {} {
    set dir [file normalize [file join [pwd] ".lint_agent_jobs"]]
    if {![file exists $dir]} {
        file mkdir $dir
    }
    return $dir
}

proc ::LintAgent::is_pid_alive {pid} {
    if {[catch {exec tasklist /FI "PID eq $pid" /FO CSV /NH} output]} {
        return 0
    }
    return [expr {[string first "\"$pid\"" $output] >= 0}]
}

proc ::LintAgent::read_file_if_exists {path} {
    if {![file exists $path]} {
        return ""
    }
    set f [open $path r]
    fconfigure $f -encoding utf-8
    set text [read $f]
    close $f
    return $text
}

proc ::LintAgent::cleanup_job {job_id} {
    variable jobs

    foreach key [array names jobs "$job_id,*"] {
        unset jobs($key)
    }
}

proc ::LintAgent::poll {job_id} {
    variable jobs

    if {![info exists jobs($job_id,pid)]} {
        return
    }

    set pid $jobs($job_id,pid)
    set output_file $jobs($job_id,output_file)

    if {[::LintAgent::is_pid_alive $pid]} {
        after 500 [list ::LintAgent::poll $job_id]
        return
    }

    set output [string trim [::LintAgent::read_file_if_exists $output_file]]
    puts ""
    if {$output eq ""} {
        puts "lint-agent finished with no output."
    } else {
        puts $output
    }

    catch {file delete -force $output_file}
    ::LintAgent::cleanup_job $job_id
}

proc ::LintAgent::call {args} {
    variable python
    variable cli
    variable url

    set parsed [::LintAgent::parse_flags $args]
    set auto_approve [lindex $parsed 0]
    set auto_reject [lindex $parsed 1]
    set prompt [lindex $parsed 2]

    set prompt_file [::LintAgent::write_prompt_file $prompt]
    set job_id [incr ::LintAgent::job_counter]
    set output_file [file normalize [file join [::LintAgent::job_dir] "lint_agent_${job_id}_[pid].out"]]

    set cmd [list $python $cli --url $url --prompt-file $prompt_file --delete-prompt-file]
    if {$auto_approve} {
        lappend cmd --auto-approve
    }
    if {$auto_reject} {
        lappend cmd --auto-reject
    }

    set exec_cmd [linsert $cmd 0 exec]
    lappend exec_cmd > $output_file 2>@1 &
    set pid_list [eval $exec_cmd]
    set pid [lindex $pid_list 0]

    set ::LintAgent::jobs($job_id,pid) $pid
    set ::LintAgent::jobs($job_id,output_file) $output_file
    after 500 [list ::LintAgent::poll $job_id]

    return ""
}

interp alias {} lint-agent {} ::LintAgent::call

puts "lint-agent command registered."
