# Register non-blocking lint-agent commands for the ALINT-PRO Tcl console.
#
# Load once in ALINT-PRO console:
#   source D:/mcp/lint_agent/langgraph_server/lint_agent_alint_console.tcl
#
# Common commands:
#   lint-agent
#   lint-agent "prompt"
#   lint-agent-new
#   lint-agent-thread
#   lint-agent-threads
#   lint-agent-resume <thread_id>
#   lint-agent-help
#
# ALINT-PRO owns the console prompt and does not hand a normal interactive
# stdin to child processes. This wrapper implements the interactive dialogue in
# Tcl and calls the Python CLI once per user turn. One-shot calls with a prompt
# still run as background jobs to keep the console responsive.

namespace eval ::LintAgent {
    variable python "D:/software/Miniconda3/envs/mcp/python.exe"
    variable cli "D:/mcp/lint_agent/langgraph_server/lint_agent_cli.py"
    variable url "http://127.0.0.1:2024"
    variable assistant "lint"
    variable thread_id ""
    variable user_id ""
    variable recursion_limit ""
    variable job_counter 0
    variable prompt_counter 0
    variable jobs
}

set ::env(PYTHONUTF8) 1
set ::env(PYTHONIOENCODING) "utf-8"
catch {fconfigure stdin -encoding utf-8}
catch {fconfigure stdout -encoding utf-8}
catch {fconfigure stderr -encoding utf-8}

proc ::LintAgent::random_hex {count} {
    set chars "0123456789abcdef"
    set out ""
    for {set i 0} {$i < $count} {incr i} {
        append out [string index $chars [expr {int(rand() * 16)}]]
    }
    return $out
}

proc ::LintAgent::new_uuid {} {
    variable python

    if {![catch {exec $python -c "import uuid; print(uuid.uuid4())"} result]} {
        set result [string trim $result]
        if {[::LintAgent::valid_uuid $result]} {
            return $result
        }
    }

    set part1 [::LintAgent::random_hex 8]
    set part2 [::LintAgent::random_hex 4]
    set part3 "4[::LintAgent::random_hex 3]"
    set variant [string index "89ab" [expr {int(rand() * 4)}]]
    set part4 "$variant[::LintAgent::random_hex 3]"
    set part5 [::LintAgent::random_hex 12]
    return "$part1-$part2-$part3-$part4-$part5"
}

proc ::LintAgent::valid_uuid {value} {
    return [regexp -nocase {^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$} [string trim $value]]
}

proc ::LintAgent::ensure_thread {} {
    variable thread_id

    if {$thread_id eq ""} {
        set thread_id [::LintAgent::new_uuid]
    }
    return $thread_id
}

proc ::LintAgent::default_user_id {} {
    if {[info exists ::env(USERNAME)] && $::env(USERNAME) ne ""} {
        return "cli:$::env(USERNAME)"
    }
    if {[info exists ::env(USER)] && $::env(USER) ne ""} {
        return "cli:$::env(USER)"
    }
    return "cli:anonymous"
}

proc ::LintAgent::active_user_id {} {
    variable user_id

    if {$user_id ne ""} {
        return $user_id
    }
    return [::LintAgent::default_user_id]
}

proc ::LintAgent::job_dir {} {
    set dir [file normalize [file join [pwd] ".lint_agent_jobs"]]
    if {![file exists $dir]} {
        file mkdir $dir
    }
    return $dir
}

proc ::LintAgent::write_prompt_file {prompt} {
    variable prompt_counter

    incr prompt_counter
    set prompt_file [file normalize [file join [::LintAgent::job_dir] "prompt_${prompt_counter}_[pid].txt"]]
    set f [open $prompt_file w]
    fconfigure $f -encoding utf-8
    puts -nonewline $f $prompt
    close $f
    return $prompt_file
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

proc ::LintAgent::is_pid_alive {pid} {
    if {[catch {exec tasklist /FI "PID eq $pid" /FO CSV /NH} output]} {
        return 0
    }
    return [expr {[string first "\"$pid\"" $output] >= 0}]
}

proc ::LintAgent::cleanup_job {job_id} {
    variable jobs

    foreach key [array names jobs "$job_id,*"] {
        unset jobs($key)
    }
}

proc ::LintAgent::running_prompt_job_for_thread {thread} {
    variable jobs

    foreach key [array names jobs "*,thread_id"] {
        set job_id [lindex [split $key ","] 0]
        if {![info exists jobs($job_id,kind)] || $jobs($job_id,kind) ne "prompt"} {
            continue
        }
        if {$jobs($key) eq $thread} {
            return $job_id
        }
    }
    return ""
}

proc ::LintAgent::poll {job_id} {
    variable jobs

    if {![info exists jobs($job_id,pid)]} {
        return
    }

    set pid $jobs($job_id,pid)
    set output_file $jobs($job_id,output_file)
    set label $jobs($job_id,label)
    set thread $jobs($job_id,thread_id)
    set announce 1
    if {[info exists jobs($job_id,announce)]} {
        set announce $jobs($job_id,announce)
    }
    set wait_var ""
    if {[info exists jobs($job_id,wait_var)]} {
        set wait_var $jobs($job_id,wait_var)
    }
    set output_label ""
    if {[info exists jobs($job_id,output_label)]} {
        set output_label $jobs($job_id,output_label)
    }

    if {[::LintAgent::is_pid_alive $pid]} {
        after 500 [list ::LintAgent::poll $job_id]
        return
    }

    set output [string trim [::LintAgent::read_file_if_exists $output_file]]
    puts ""
    if {$announce} {
        puts "lint-agent job $job_id finished: $label"
        puts "thread_id: $thread"
    }
    if {$output eq ""} {
        puts "lint-agent finished with no output."
    } else {
        if {$output_label ne ""} {
            puts "$output_label:"
        }
        puts $output
    }

    catch {file delete -force $output_file}
    ::LintAgent::cleanup_job $job_id
    if {$wait_var ne ""} {
        set $wait_var 1
    }
}

proc ::LintAgent::base_cli_cmd {thread run_user run_url run_recursion} {
    variable python
    variable cli
    variable assistant

    set cmd [list $python $cli --url $run_url --assistant $assistant --thread-id $thread]
    if {$run_user ne ""} {
        lappend cmd --user-id $run_user
    }
    if {$run_recursion ne ""} {
        lappend cmd --recursion-limit $run_recursion
    }
    return $cmd
}

proc ::LintAgent::start_background_job {cmd label thread kind {announce 1}} {
    variable job_counter
    variable jobs

    incr job_counter
    set job_id $job_counter
    set output_file [file normalize [file join [::LintAgent::job_dir] "lint_agent_${job_id}_[pid].out"]]
    if {[catch {set pid_list [exec {*}$cmd > $output_file 2>@1 &]} err]} {
        catch {file delete -force $output_file}
        error "failed to start lint-agent: $err"
    }

    set pid [lindex $pid_list 0]
    set jobs($job_id,pid) $pid
    set jobs($job_id,output_file) $output_file
    set jobs($job_id,label) $label
    set jobs($job_id,thread_id) $thread
    set jobs($job_id,kind) $kind
    set jobs($job_id,announce) $announce

    if {$announce} {
        puts "lint-agent job $job_id started: $label"
        puts "thread_id: $thread"
    }
    after 500 [list ::LintAgent::poll $job_id]
    return $job_id
}

proc ::LintAgent::run_background {cmd label thread kind} {
    ::LintAgent::start_background_job $cmd $label $thread $kind 1
    return ""
}

proc ::LintAgent::run_sync {cmd} {
    variable job_counter

    incr job_counter
    set output_file [file normalize [file join [::LintAgent::job_dir] "lint_agent_sync_${job_counter}_[pid].out"]]
    set status 0
    set exec_error ""

    if {[catch {exec {*}$cmd > $output_file 2>@1} err]} {
        set status 1
        set exec_error [string trim $err]
    }

    set output [string trim [::LintAgent::read_file_if_exists $output_file]]
    if {$output ne ""} {
        puts $output
    } elseif {$exec_error ne ""} {
        puts $exec_error
    }

    catch {file delete -force $output_file}
    return $status
}

proc ::LintAgent::run_sync_prompt {prompt auto_approve auto_reject run_user run_url run_recursion} {
    set thread [::LintAgent::ensure_thread]
    set active_job [::LintAgent::running_prompt_job_for_thread $thread]
    if {$active_job ne ""} {
        puts "thread $thread already has running lint-agent job $active_job; wait for it or use /new"
        return 1
    }

    set prompt_file [::LintAgent::write_prompt_file $prompt]
    set cmd [::LintAgent::base_cli_cmd $thread $run_user $run_url $run_recursion]
    lappend cmd --prompt-file $prompt_file --delete-prompt-file
    if {$auto_approve} {
        lappend cmd --auto-approve
    }
    if {$auto_reject} {
        lappend cmd --auto-reject
    }
    return [::LintAgent::run_sync $cmd]
}

proc ::LintAgent::run_dialog_prompt {prompt auto_approve auto_reject run_user run_url run_recursion} {
    variable jobs

    set thread [::LintAgent::ensure_thread]
    set active_job [::LintAgent::running_prompt_job_for_thread $thread]
    if {$active_job ne ""} {
        puts "thread $thread already has running lint-agent job $active_job; wait for it or use /new"
        return 1
    }

    set prompt_file [::LintAgent::write_prompt_file $prompt]
    set cmd [::LintAgent::base_cli_cmd $thread $run_user $run_url $run_recursion]
    lappend cmd --prompt-file $prompt_file --delete-prompt-file
    if {$auto_approve} {
        lappend cmd --auto-approve
    }
    if {$auto_reject} {
        lappend cmd --auto-reject
    }

    set job_id [::LintAgent::start_background_job $cmd "prompt" $thread "prompt" 0]
    set wait_var "::LintAgent::dialog_done_$job_id"
    set jobs($job_id,wait_var) $wait_var
    set jobs($job_id,output_label) "assistant"
    vwait $wait_var
    catch {unset $wait_var}
    return 0
}

proc ::LintAgent::run_sync_repl_command {slash_command run_user run_url run_recursion} {
    set thread [::LintAgent::ensure_thread]
    set cmd [::LintAgent::base_cli_cmd $thread $run_user $run_url $run_recursion]
    lappend cmd --repl-command $slash_command
    return [::LintAgent::run_sync $cmd]
}

proc ::LintAgent::parse_flags {arg_list} {
    set auto_approve 0
    set auto_reject 0
    set thread_override ""
    set user_override ""
    set url_override ""
    set recursion_override ""
    set switch_current_thread 0
    set prompt_parts {}

    set i 0
    set argc [llength $arg_list]
    while {$i < $argc} {
        set arg [lindex $arg_list $i]
        switch -- $arg {
            "-auto-approve" -
            "--auto-approve" {
                set auto_approve 1
            }
            "-auto-reject" -
            "--auto-reject" {
                set auto_reject 1
            }
            "-new" -
            "--new" {
                set thread_override [::LintAgent::new_uuid]
                set switch_current_thread 1
            }
            "-thread" -
            "--thread-id" {
                incr i
                if {$i >= $argc} {
                    error "$arg requires a UUID thread_id"
                }
                set thread_override [lindex $arg_list $i]
            }
            "-user" -
            "--user-id" {
                incr i
                if {$i >= $argc} {
                    error "$arg requires a user_id"
                }
                set user_override [lindex $arg_list $i]
            }
            "-url" -
            "--url" {
                incr i
                if {$i >= $argc} {
                    error "$arg requires a URL"
                }
                set url_override [lindex $arg_list $i]
            }
            "-recursion-limit" -
            "--recursion-limit" {
                incr i
                if {$i >= $argc} {
                    error "$arg requires a number"
                }
                set recursion_override [lindex $arg_list $i]
            }
            default {
                lappend prompt_parts $arg
            }
        }
        incr i
    }

    set prompt [join $prompt_parts " "]
    if {$auto_approve && $auto_reject} {
        error "-auto-approve and -auto-reject cannot be used together"
    }

    return [list $auto_approve $auto_reject $thread_override $user_override $url_override $recursion_override $switch_current_thread $prompt]
}

proc ::LintAgent::resolve_call_options {thread_override user_override url_override recursion_override switch_current_thread {dialog_mode 0}} {
    variable url
    variable user_id
    variable thread_id
    variable recursion_limit

    if {$thread_override ne ""} {
        set run_thread [string trim $thread_override]
        if {![::LintAgent::valid_uuid $run_thread]} {
            error "thread_id must be a UUID. Use lint-agent-threads to list valid thread IDs."
        }
    } else {
        set run_thread [::LintAgent::ensure_thread]
    }
    if {$switch_current_thread || $dialog_mode} {
        set thread_id $run_thread
    }

    if {$user_override ne ""} {
        set run_user $user_override
    } else {
        set run_user $user_id
    }
    if {$url_override ne ""} {
        set run_url $url_override
    } else {
        set run_url $url
    }
    if {$recursion_override ne ""} {
        set run_recursion $recursion_override
    } else {
        set run_recursion $recursion_limit
    }

    return [list $run_thread $run_user $run_url $run_recursion]
}

proc ::LintAgent::dialog_help {} {
    puts "commands:"
    puts {  /new                 start a new persistent thread}
    puts {  /threads [limit]     list recent threads for the current user}
    puts {  /threads all [limit] list recent threads without user filtering}
    puts {  /resume <thread_id>  switch to an existing persistent thread}
    puts {  /thread              show the current thread_id}
    puts {  /thread-info         show current thread metadata}
    puts {  /state               show current thread state summary}
    puts {  /history [limit]     show current thread checkpoint history}
    puts {  /runs [limit]        list runs on current thread}
    puts {  /run <run_id>        show one run as JSON}
    puts {  /cancel <run_id>     cancel a pending/running run}
    puts {  /assistants [limit]  list assistants for this graph}
    puts {  /assistant [id]      show assistant metadata}
    puts {  /graph               show assistant graph JSON}
    puts {  /schemas             show assistant schemas JSON}
    puts {  /help                show this help}
    puts {  /exit                leave interactive mode}
    return ""
}

proc ::LintAgent::dialog {auto_approve auto_reject thread_override user_override url_override recursion_override switch_current_thread} {
    variable assistant
    variable thread_id

    set resolved [::LintAgent::resolve_call_options $thread_override $user_override $url_override $recursion_override $switch_current_thread 1]
    lassign $resolved run_thread run_user run_url run_recursion
    set display_user $run_user
    if {$display_user eq ""} {
        set display_user [::LintAgent::default_user_id]
    }

    puts "lint-agent interactive mode"
    puts "server: $run_url"
    puts "assistant: $assistant"
    puts "thread_id: $thread_id"
    puts "user_id: $display_user"
    puts "commands: /new, /threads, /resume <thread_id>, /thread, /help, /exit"
    puts ""

    while {1} {
        puts -nonewline "lint-agent> "
        flush stdout
        if {[gets stdin line] < 0} {
            puts ""
            return ""
        }

        set prompt [string trim $line]
        if {$prompt eq ""} {
            continue
        }

        set lowered [string tolower $prompt]
        if {$lowered in {"/exit" "/quit" "exit" "quit" "q"}} {
            return ""
        }
        if {$lowered eq "/help"} {
            ::LintAgent::dialog_help
            continue
        }
        if {$lowered eq "/new"} {
            set thread_id [::LintAgent::new_uuid]
            puts "started new thread: $thread_id"
            continue
        }
        if {$lowered eq "/thread"} {
            puts $thread_id
            continue
        }
        if {$lowered eq "/resume" || [string match "/resume *" $lowered]} {
            set new_thread_id ""
            if {[llength [split $prompt]] >= 2} {
                set new_thread_id [lindex [split $prompt] 1]
            }
            if {$new_thread_id eq ""} {
                puts "usage: /resume <thread_id>"
                continue
            }
            if {![::LintAgent::valid_uuid $new_thread_id]} {
                puts "thread_id must be a UUID. Use /threads to list valid thread IDs."
                continue
            }
            set thread_id $new_thread_id
            puts "resumed thread: $thread_id"
            continue
        }
        if {[string index $prompt 0] eq "/"} {
            ::LintAgent::run_sync_repl_command $prompt $run_user $run_url $run_recursion
            continue
        }

        puts ""
        puts "user: $prompt"
        flush stdout
        ::LintAgent::run_dialog_prompt $prompt $auto_approve $auto_reject $run_user $run_url $run_recursion
        puts ""
    }
}

proc ::LintAgent::call {args} {
    set parsed [::LintAgent::parse_flags $args]
    lassign $parsed auto_approve auto_reject thread_override user_override url_override recursion_override switch_current_thread prompt

    if {[string trim $prompt] eq ""} {
        return [::LintAgent::dialog $auto_approve $auto_reject $thread_override $user_override $url_override $recursion_override $switch_current_thread]
    }

    set resolved [::LintAgent::resolve_call_options $thread_override $user_override $url_override $recursion_override $switch_current_thread 0]
    lassign $resolved run_thread run_user run_url run_recursion

    set active_job [::LintAgent::running_prompt_job_for_thread $run_thread]
    if {$active_job ne ""} {
        error "thread $run_thread already has running lint-agent job $active_job; wait for it or use lint-agent-new"
    }

    set prompt_file [::LintAgent::write_prompt_file $prompt]
    set cmd [::LintAgent::base_cli_cmd $run_thread $run_user $run_url $run_recursion]
    lappend cmd --prompt-file $prompt_file --delete-prompt-file
    if {$auto_approve} {
        lappend cmd --auto-approve
    }
    if {$auto_reject} {
        lappend cmd --auto-reject
    }

    return [::LintAgent::run_background $cmd "prompt" $run_thread "prompt"]
}

proc ::LintAgent::run_repl_command {slash_command label} {
    variable url
    variable user_id
    variable recursion_limit

    set thread [::LintAgent::ensure_thread]
    set cmd [::LintAgent::base_cli_cmd $thread $user_id $url $recursion_limit]
    lappend cmd --repl-command $slash_command
    return [::LintAgent::run_background $cmd $label $thread "command"]
}

proc ::LintAgent::new_thread {} {
    variable thread_id

    set thread_id [::LintAgent::new_uuid]
    puts "started new lint-agent thread: $thread_id"
    return ""
}

proc ::LintAgent::resume {new_thread_id} {
    variable thread_id

    set new_thread_id [string trim $new_thread_id]
    if {![::LintAgent::valid_uuid $new_thread_id]} {
        error "thread_id must be a UUID. Use lint-agent-threads to list valid thread IDs."
    }
    set thread_id $new_thread_id
    puts "resumed lint-agent thread: $thread_id"
    return ""
}

proc ::LintAgent::thread {} {
    puts "thread_id: [::LintAgent::ensure_thread]"
    puts "user_id: [::LintAgent::active_user_id]"
    return ""
}

proc ::LintAgent::threads {args} {
    set command "/threads"
    if {[llength $args] > 0} {
        append command " " [join $args " "]
    }
    return [::LintAgent::run_repl_command $command "threads"]
}

proc ::LintAgent::thread_info {} {
    return [::LintAgent::run_repl_command "/thread-info" "thread-info"]
}

proc ::LintAgent::state {} {
    return [::LintAgent::run_repl_command "/state" "state"]
}

proc ::LintAgent::history {args} {
    set command "/history"
    if {[llength $args] > 0} {
        append command " " [join $args " "]
    }
    return [::LintAgent::run_repl_command $command "history"]
}

proc ::LintAgent::runs {args} {
    set command "/runs"
    if {[llength $args] > 0} {
        append command " " [join $args " "]
    }
    return [::LintAgent::run_repl_command $command "runs"]
}

proc ::LintAgent::assistant_info {args} {
    set command "/assistant"
    if {[llength $args] > 0} {
        append command " " [join $args " "]
    }
    return [::LintAgent::run_repl_command $command "assistant"]
}

proc ::LintAgent::graph {} {
    return [::LintAgent::run_repl_command "/graph" "graph"]
}

proc ::LintAgent::schemas {} {
    return [::LintAgent::run_repl_command "/schemas" "schemas"]
}

proc ::LintAgent::user {args} {
    variable user_id

    if {[llength $args] == 0} {
        puts "user_id: [::LintAgent::active_user_id]"
        if {$user_id eq ""} {
            puts "source: default Python CLI user"
        }
        return ""
    }
    if {[llength $args] != 1} {
        error {usage: lint-agent-user ?user_id|-default?}
    }
    set value [lindex $args 0]
    if {$value eq "-default"} {
        set user_id ""
    } else {
        set user_id $value
    }
    puts "user_id: [::LintAgent::active_user_id]"
    return ""
}

proc ::LintAgent::set_url {args} {
    variable url

    if {[llength $args] == 0} {
        puts "url: $url"
        return ""
    }
    if {[llength $args] != 1} {
        error {usage: lint-agent-url ?url?}
    }
    set url [lindex $args 0]
    puts "url: $url"
    return ""
}

proc ::LintAgent::set_assistant {args} {
    variable assistant

    if {[llength $args] == 0} {
        puts "assistant: $assistant"
        return ""
    }
    if {[llength $args] != 1} {
        error {usage: lint-agent-assistant ?name?}
    }
    set assistant [lindex $args 0]
    puts "assistant: $assistant"
    return ""
}

proc ::LintAgent::set_recursion_limit {args} {
    variable recursion_limit

    if {[llength $args] == 0} {
        if {$recursion_limit eq ""} {
            puts "recursion_limit: Python CLI default"
        } else {
            puts "recursion_limit: $recursion_limit"
        }
        return ""
    }
    if {[llength $args] != 1} {
        error {usage: lint-agent-recursion-limit ?number|-default?}
    }
    set value [lindex $args 0]
    if {$value eq "-default"} {
        set recursion_limit ""
    } else {
        set recursion_limit $value
    }
    return [::LintAgent::set_recursion_limit]
}

proc ::LintAgent::jobs {} {
    variable jobs

    set ids {}
    foreach key [array names jobs "*,pid"] {
        lappend ids [lindex [split $key ","] 0]
    }
    set ids [lsort -integer -unique $ids]
    if {[llength $ids] == 0} {
        puts "no lint-agent jobs running"
        return ""
    }
    foreach job_id $ids {
        puts "job $job_id pid=$jobs($job_id,pid) kind=$jobs($job_id,kind) label=$jobs($job_id,label) thread=$jobs($job_id,thread_id)"
    }
    return ""
}

proc ::LintAgent::help {} {
    puts "lint-agent commands:"
    puts "  lint-agent                                  enter interactive dialogue mode"
    puts "  lint-agent \"prompt\"                       run one non-blocking prompt on current thread"
    puts "  lint-agent -auto-approve                   enter interactive mode with HITL auto-approve"
    puts "  lint-agent -auto-reject                    enter interactive mode with HITL auto-reject"
    puts "  lint-agent -new \"prompt\"                  start a new thread and send prompt"
    puts "  lint-agent -thread <uuid> \"prompt\"        send prompt to a specific thread"
    puts "  lint-agent -auto-approve \"prompt\"         approve HITL tool requests"
    puts "  lint-agent -auto-reject \"prompt\"          reject HITL tool requests"
    puts "  lint-agent-new                            switch to a new empty thread"
    puts "  lint-agent-thread                         show current thread and user"
    puts "  lint-agent-resume <uuid>                  switch to an existing thread"
    puts "  lint-agent-threads ?all? ?limit?          list threads through LangGraph SDK"
    puts "  lint-agent-state                          show current thread state"
    puts "  lint-agent-history ?limit?                show checkpoint history"
    puts "  lint-agent-runs ?limit?                   list runs on current thread"
    puts "  lint-agent-thread-info                    show thread metadata"
    puts "  lint-agent-user ?user_id|-default?        show or set user_id"
    puts "  lint-agent-url ?url?                      show or set Agent Server URL"
    puts "  lint-agent-jobs                           list running background jobs"
    return ""
}

::LintAgent::ensure_thread

interp alias {} lint-agent {} ::LintAgent::call
interp alias {} lint-agent-help {} ::LintAgent::help
interp alias {} lint-agent-new {} ::LintAgent::new_thread
interp alias {} lint-agent-thread {} ::LintAgent::thread
interp alias {} lint-agent-resume {} ::LintAgent::resume
interp alias {} lint-agent-threads {} ::LintAgent::threads
interp alias {} lint-agent-thread-info {} ::LintAgent::thread_info
interp alias {} lint-agent-state {} ::LintAgent::state
interp alias {} lint-agent-history {} ::LintAgent::history
interp alias {} lint-agent-runs {} ::LintAgent::runs
interp alias {} lint-agent-assistant {} ::LintAgent::assistant_info
interp alias {} lint-agent-graph {} ::LintAgent::graph
interp alias {} lint-agent-schemas {} ::LintAgent::schemas
interp alias {} lint-agent-user {} ::LintAgent::user
interp alias {} lint-agent-url {} ::LintAgent::set_url
interp alias {} lint-agent-assistant-name {} ::LintAgent::set_assistant
interp alias {} lint-agent-recursion-limit {} ::LintAgent::set_recursion_limit
interp alias {} lint-agent-jobs {} ::LintAgent::jobs

puts "lint-agent command registered. Use lint-agent-help for commands."
puts "lint-agent thread_id: $::LintAgent::thread_id"
