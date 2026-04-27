# Register lint-agent commands for the ALINT-PRO Tcl console.
#
# Load once in ALINT-PRO console:
#   source D:/Downloads/alint-pro-customer/lint_agent/langgraph_server/lint_agent_alint_console.tcl
#
# This wrapper talks directly to a LangGraph Agent Server over HTTP. It uses
# only Tcl core socket commands because ALINT-PRO Tcl does not provide a
# complete Tcllib http package. It does not require Python, langgraph-sdk, or
# the Python CLI on the EDA workstation.

namespace eval ::LintAgent {
    if {[info exists ::env(LANGGRAPH_URL)] && [string trim $::env(LANGGRAPH_URL)] ne ""} {
        variable url $::env(LANGGRAPH_URL)
    } else {
        variable url "http://127.0.0.1:2024"
    }
    if {[info exists ::env(LANGGRAPH_ASSISTANT)] && [string trim $::env(LANGGRAPH_ASSISTANT)] ne ""} {
        variable assistant $::env(LANGGRAPH_ASSISTANT)
    } else {
        variable assistant "lint"
    }
    variable thread_id ""
    variable user_id ""
    variable recursion_limit "50"
    variable request_timeout 1200000
}

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

proc ::LintAgent::json_escape {text} {
    set out ""
    set len [string length $text]
    for {set i 0} {$i < $len} {incr i} {
        set ch [string index $text $i]
        scan $ch %c code
        switch -- $ch {
            "\"" {append out "\\\""}
            "\\" {append out "\\\\"}
            "\b" {append out "\\b"}
            "\f" {append out "\\f"}
            "\n" {append out "\\n"}
            "\r" {append out "\\r"}
            "\t" {append out "\\t"}
            default {
                if {$code < 32} {
                    append out [format "\\u%04x" $code]
                } else {
                    append out $ch
                }
            }
        }
    }
    return $out
}

proc ::LintAgent::json_string {text} {
    return "\"[::LintAgent::json_escape $text]\""
}

proc ::LintAgent::json_bool {value} {
    if {$value} {
        return "true"
    }
    return "false"
}

proc ::LintAgent::json_metadata {run_user} {
    variable assistant

    set authenticated [expr {$run_user ne ""}]
    if {$run_user eq ""} {
        set run_user [::LintAgent::default_user_id]
    }
    return "{\"source\":\"lint-agent-tcl\",\"assistant\":[::LintAgent::json_string $assistant],\"user_id\":[::LintAgent::json_string $run_user],\"authenticated\":[::LintAgent::json_bool $authenticated]}"
}

proc ::LintAgent::json_context {thread run_user} {
    set authenticated [expr {$run_user ne ""}]
    if {$run_user eq ""} {
        set run_user [::LintAgent::default_user_id]
    }
    return "{\"user_id\":[::LintAgent::json_string $run_user],\"thread_id\":[::LintAgent::json_string $thread],\"authenticated\":[::LintAgent::json_bool $authenticated]}"
}

proc ::LintAgent::json_run_payload {thread prompt run_user run_recursion} {
    variable assistant

    set body "{"
    append body "\"assistant_id\":[::LintAgent::json_string $assistant],"
    append body "\"input\":{\"messages\":\[{\"role\":\"user\",\"content\":[::LintAgent::json_string $prompt]}\]},"
    append body "\"metadata\":[::LintAgent::json_metadata $run_user],"
    append body "\"context\":[::LintAgent::json_context $thread $run_user],"
    append body "\"if_not_exists\":\"create\""
    if {$run_recursion ne ""} {
        append body ",\"config\":{\"recursion_limit\":$run_recursion}"
    }
    append body "}"
    return $body
}

proc ::LintAgent::json_thread_payload {thread run_user} {
    variable assistant

    set body "{"
    append body "\"thread_id\":[::LintAgent::json_string $thread],"
    append body "\"metadata\":[::LintAgent::json_metadata $run_user],"
    append body "\"if_exists\":\"do_nothing\","
    append body "\"graph_id\":[::LintAgent::json_string $assistant]"
    append body "}"
    return $body
}

proc ::LintAgent::json_search_threads_payload {include_all limit} {
    set body "{"
    if {!$include_all} {
        append body "\"metadata\":{\"user_id\":[::LintAgent::json_string [::LintAgent::active_user_id]]},"
    }
    append body "\"limit\":$limit,"
    append body "\"sort_by\":\"updated_at\","
    append body "\"sort_order\":\"desc\","
    append body "\"select\":\[\"thread_id\",\"created_at\",\"updated_at\",\"metadata\",\"status\"\]"
    append body "}"
    return $body
}

proc ::LintAgent::parse_http_url {run_url path} {
    set run_url [string trim $run_url]
    if {![regexp -nocase {^http://([^/:?#]+)(:([0-9]+))?([^?#]*)?} $run_url -> host _ port base_path]} {
        error "unsupported Agent Server URL '$run_url'; ALINT Tcl client supports plain http://host[:port][/base-path]"
    }
    if {$port eq ""} {
        set port 80
    }
    set base_path [string trimright $base_path "/"]
    if {$base_path eq ""} {
        set request_path $path
    } else {
        set request_path "$base_path$path"
    }
    if {[string index $request_path 0] ne "/"} {
        set request_path "/$request_path"
    }
    set host_header $host
    if {$port != 80} {
        set host_header "$host:$port"
    }
    return [list $host $port $host_header $request_path]
}

proc ::LintAgent::http_build_request {method host_header request_path body} {
    set body_bytes ""
    if {$body ne ""} {
        set body_bytes [encoding convertto utf-8 $body]
    }

    set request "$method $request_path HTTP/1.1\r\n"
    append request "Host: $host_header\r\n"
    append request "Accept: application/json\r\n"
    append request "Connection: close\r\n"
    if {$body ne ""} {
        append request "Content-Type: application/json; charset=utf-8\r\n"
        append request "Content-Length: [string length $body_bytes]\r\n"
    }
    append request "\r\n"
    append request $body_bytes
    return $request
}

proc ::LintAgent::http_decode_chunked {data} {
    set out ""
    set idx 0
    set len [string length $data]

    while {$idx < $len} {
        set line_end [string first "\r\n" $data $idx]
        set sep_len 2
        if {$line_end < 0} {
            set line_end [string first "\n" $data $idx]
            set sep_len 1
        }
        if {$line_end < 0} {
            break
        }

        set line [string trim [string range $data $idx [expr {$line_end - 1}]]]
        set semi [string first ";" $line]
        if {$semi >= 0} {
            set line [string range $line 0 [expr {$semi - 1}]]
        }
        if {[scan $line %x chunk_size] != 1} {
            error "invalid chunked HTTP response"
        }

        set idx [expr {$line_end + $sep_len}]
        if {$chunk_size == 0} {
            break
        }
        append out [string range $data $idx [expr {$idx + $chunk_size - 1}]]
        set idx [expr {$idx + $chunk_size}]
        if {[string range $data $idx [expr {$idx + 1}]] eq "\r\n"} {
            incr idx 2
        } elseif {[string index $data $idx] eq "\n"} {
            incr idx
        }
    }

    return $out
}

proc ::LintAgent::http_parse_response {raw method path} {
    set header_end [string first "\r\n\r\n" $raw]
    set sep_len 4
    if {$header_end < 0} {
        set header_end [string first "\n\n" $raw]
        set sep_len 2
    }
    if {$header_end < 0} {
        error "HTTP $method $path failed: invalid response"
    }

    set header_text [string range $raw 0 [expr {$header_end - 1}]]
    set body_bytes [string range $raw [expr {$header_end + $sep_len}] end]
    set lines [split [string map [list "\r\n" "\n"] $header_text] "\n"]
    set status_line [lindex $lines 0]
    if {![regexp {^HTTP/[0-9.]+\s+([0-9]+)} $status_line -> ncode]} {
        error "HTTP $method $path failed: invalid status line '$status_line'"
    }

    set headers [dict create]
    foreach line [lrange $lines 1 end] {
        set colon [string first ":" $line]
        if {$colon < 0} {
            continue
        }
        set key [string tolower [string trim [string range $line 0 [expr {$colon - 1}]]]]
        set value [string trim [string range $line [expr {$colon + 1}] end]]
        dict set headers $key $value
    }

    if {[dict exists $headers "transfer-encoding"] && [string match -nocase "*chunked*" [dict get $headers "transfer-encoding"]]} {
        set body_bytes [::LintAgent::http_decode_chunked $body_bytes]
    }

    if {[catch {set data [encoding convertfrom utf-8 $body_bytes]}]} {
        set data $body_bytes
    }
    return [list $ncode $data $status_line]
}

proc ::LintAgent::http_response_complete {raw} {
    set header_end [string first "\r\n\r\n" $raw]
    set sep_len 4
    if {$header_end < 0} {
        set header_end [string first "\n\n" $raw]
        set sep_len 2
    }
    if {$header_end < 0} {
        return 0
    }

    set header_text [string range $raw 0 [expr {$header_end - 1}]]
    set body_bytes [string range $raw [expr {$header_end + $sep_len}] end]
    set lines [split [string map [list "\r\n" "\n"] $header_text] "\n"]
    set headers [dict create]
    foreach line [lrange $lines 1 end] {
        set colon [string first ":" $line]
        if {$colon < 0} {
            continue
        }
        set key [string tolower [string trim [string range $line 0 [expr {$colon - 1}]]]]
        set value [string trim [string range $line [expr {$colon + 1}] end]]
        dict set headers $key $value
    }

    if {[dict exists $headers "content-length"]} {
        set expected [dict get $headers "content-length"]
        if {[string is integer -strict $expected]} {
            return [expr {[string length $body_bytes] >= $expected}]
        }
    }
    if {[dict exists $headers "transfer-encoding"] && [string match -nocase "*chunked*" [dict get $headers "transfer-encoding"]]} {
        return [regexp -nocase {(^|\r\n|\n)0[ \t]*(;[^\r\n]*)?(\r\n\r\n|\n\n|\r\n\n|\n\r\n)} $body_bytes]
    }
    return 0
}

proc ::LintAgent::http_request_sync {method run_url path {body ""}} {
    variable request_timeout

    lassign [::LintAgent::parse_http_url $run_url $path] host port host_header request_path
    set request [::LintAgent::http_build_request $method $host_header $request_path $body]

    if {[catch {set sock [socket $host $port]} err]} {
        error "HTTP $method $path failed: cannot connect to $host_header: $err"
    }
    fconfigure $sock -translation binary -encoding binary -buffering none
    if {[catch {
        puts -nonewline $sock $request
        flush $sock
    } err]} {
        catch {close $sock}
        error "HTTP $method $path failed: $err"
    }

    # ALINT-PRO Tcl exposes fileevent, but it is not reliable in alintcon batch
    # execution. Mirror ocli_la.tcl: non-blocking reads plus update/after polling.
    fconfigure $sock -blocking 0
    set raw ""
    set started [clock milliseconds]
    while {1} {
        if {[catch {set chunk [read $sock]} err]} {
            catch {close $sock}
            error "HTTP $method $path failed: $err"
        }
        if {$chunk ne ""} {
            append raw $chunk
        }
        if {[::LintAgent::http_response_complete $raw]} {
            break
        }
        if {[eof $sock]} {
            break
        }
        if {[expr {[clock milliseconds] - $started}] > $request_timeout} {
            catch {close $sock}
            error "HTTP $method $path failed: request timed out"
        }
        catch {update}
        after 10
    }
    close $sock

    lassign [::LintAgent::http_parse_response $raw $method $path] ncode data status_line
    if {$ncode < 200 || $ncode >= 300} {
        error "HTTP $method $path failed: code=$ncode body=$data"
    }
    return $data
}

proc ::LintAgent::json_skip_ws {json_var idx_var} {
    upvar 1 $json_var json $idx_var idx
    set len [string length $json]
    while {$idx < $len} {
        set ch [string index $json $idx]
        if {$ch ni {" " "\t" "\r" "\n"}} {
            break
        }
        incr idx
    }
}

proc ::LintAgent::json_parse_string {json_var idx_var} {
    upvar 1 $json_var json $idx_var idx
    incr idx
    set out ""
    set len [string length $json]
    while {$idx < $len} {
        set ch [string index $json $idx]
        incr idx
        if {$ch eq "\""} {
            return $out
        }
        if {$ch ne "\\"} {
            append out $ch
            continue
        }
        if {$idx >= $len} {
            error "invalid JSON string escape"
        }
        set esc [string index $json $idx]
        incr idx
        switch -- $esc {
            "\"" {append out "\""}
            "\\" {append out "\\"}
            "/" {append out "/"}
            "b" {append out "\b"}
            "f" {append out "\f"}
            "n" {append out "\n"}
            "r" {append out "\r"}
            "t" {append out "\t"}
            "u" {
                if {$idx + 3 >= $len} {
                    error "invalid JSON unicode escape"
                }
                set hex [string range $json $idx [expr {$idx + 3}]]
                incr idx 4
                if {![regexp -nocase {^[0-9a-f]{4}$} $hex]} {
                    error "invalid JSON unicode escape"
                }
                scan $hex %x code
                append out [format %c $code]
            }
            default {error "invalid JSON escape \\$esc"}
        }
    }
    error "unterminated JSON string"
}

proc ::LintAgent::json_parse_literal {json_var idx_var literal type value} {
    upvar 1 $json_var json $idx_var idx
    set end [expr {$idx + [string length $literal] - 1}]
    if {[string range $json $idx $end] ne $literal} {
        error "invalid JSON literal"
    }
    set idx [expr {$end + 1}]
    return [list $type $value]
}

proc ::LintAgent::json_parse_number {json_var idx_var} {
    upvar 1 $json_var json $idx_var idx
    set start $idx
    set len [string length $json]
    while {$idx < $len} {
        set ch [string index $json $idx]
        if {![regexp {[-+0-9.eE]} $ch]} {
            break
        }
        incr idx
    }
    return [list number [string range $json $start [expr {$idx - 1}]]]
}

proc ::LintAgent::json_parse_array {json_var idx_var} {
    upvar 1 $json_var json $idx_var idx
    incr idx
    set values {}
    ::LintAgent::json_skip_ws json idx
    if {[string index $json $idx] eq "\]"} {
        incr idx
        return [list array $values]
    }
    while {1} {
        lappend values [::LintAgent::json_parse_value json idx]
        ::LintAgent::json_skip_ws json idx
        set ch [string index $json $idx]
        if {$ch eq ","} {
            incr idx
            continue
        }
        if {$ch eq "\]"} {
            incr idx
            return [list array $values]
        }
        error "expected , or \] in JSON array"
    }
}

proc ::LintAgent::json_parse_object {json_var idx_var} {
    upvar 1 $json_var json $idx_var idx
    incr idx
    set result [dict create]
    ::LintAgent::json_skip_ws json idx
    if {[string index $json $idx] eq "\}"} {
        incr idx
        return [list object $result]
    }
    while {1} {
        ::LintAgent::json_skip_ws json idx
        if {[string index $json $idx] ne "\""} {
            error "expected JSON object key"
        }
        set key [::LintAgent::json_parse_string json idx]
        ::LintAgent::json_skip_ws json idx
        if {[string index $json $idx] ne ":"} {
            error "expected : after JSON object key"
        }
        incr idx
        set value [::LintAgent::json_parse_value json idx]
        dict set result $key $value
        ::LintAgent::json_skip_ws json idx
        set ch [string index $json $idx]
        if {$ch eq ","} {
            incr idx
            continue
        }
        if {$ch eq "\}"} {
            incr idx
            return [list object $result]
        }
        error "expected , or \} in JSON object"
    }
}

proc ::LintAgent::json_parse_value {json_var idx_var} {
    upvar 1 $json_var json $idx_var idx
    ::LintAgent::json_skip_ws json idx
    set ch [string index $json $idx]
    switch -- $ch {
        "\"" {return [list string [::LintAgent::json_parse_string json idx]]}
        "\{" {return [::LintAgent::json_parse_object json idx]}
        "\[" {return [::LintAgent::json_parse_array json idx]}
        "t" {return [::LintAgent::json_parse_literal json idx true bool 1]}
        "f" {return [::LintAgent::json_parse_literal json idx false bool 0]}
        "n" {return [::LintAgent::json_parse_literal json idx null null ""]}
        default {return [::LintAgent::json_parse_number json idx]}
    }
}

proc ::LintAgent::json_parse {json} {
    set idx 0
    set value [::LintAgent::json_parse_value json idx]
    ::LintAgent::json_skip_ws json idx
    return $value
}

proc ::LintAgent::json_type {value} {
    return [lindex $value 0]
}

proc ::LintAgent::json_unwrap {value} {
    return [lindex $value 1]
}

proc ::LintAgent::json_get {value key {default ""}} {
    if {[::LintAgent::json_type $value] ne "object"} {
        return $default
    }
    set dict_value [::LintAgent::json_unwrap $value]
    if {![dict exists $dict_value $key]} {
        return $default
    }
    return [dict get $dict_value $key]
}

proc ::LintAgent::json_string_value {value {default ""}} {
    if {$value eq ""} {
        return $default
    }
    set type [::LintAgent::json_type $value]
    if {$type in {"string" "number" "bool"}} {
        return [::LintAgent::json_unwrap $value]
    }
    return $default
}

proc ::LintAgent::message_text {message} {
    set content [::LintAgent::json_get $message "content"]
    if {$content eq ""} {
        return ""
    }
    if {[::LintAgent::json_type $content] eq "string"} {
        return [::LintAgent::json_unwrap $content]
    }
    if {[::LintAgent::json_type $content] eq "array"} {
        set out ""
        foreach item [::LintAgent::json_unwrap $content] {
            if {[::LintAgent::json_type $item] ne "object"} {
                continue
            }
            set text [::LintAgent::json_get $item "text"]
            if {$text eq ""} {
                set text [::LintAgent::json_get $item "content"]
            }
            append out [::LintAgent::json_string_value $text]
        }
        return $out
    }
    return ""
}

proc ::LintAgent::last_ai_text {json_text} {
    if {[catch {set parsed [::LintAgent::json_parse $json_text]}]} {
        return ""
    }
    set messages [::LintAgent::json_get $parsed "messages"]
    if {$messages eq ""} {
        set values [::LintAgent::json_get $parsed "values"]
        set messages [::LintAgent::json_get $values "messages"]
    }
    if {$messages eq "" || [::LintAgent::json_type $messages] ne "array"} {
        return ""
    }
    set items [::LintAgent::json_unwrap $messages]
    for {set i [expr {[llength $items] - 1}]} {$i >= 0} {incr i -1} {
        set message [lindex $items $i]
        set role [string tolower [::LintAgent::json_string_value [::LintAgent::json_get $message "type"]]]
        if {$role eq ""} {
            set role [string tolower [::LintAgent::json_string_value [::LintAgent::json_get $message "role"]]]
        }
        if {$role in {"ai" "assistant"}} {
            set text [string trim [::LintAgent::message_text $message]]
            if {$text ne ""} {
                return $text
            }
        }
    }
    return ""
}

proc ::LintAgent::print_thread_search {json_text current_thread} {
    if {[catch {set parsed [::LintAgent::json_parse $json_text]}]} {
        puts $json_text
        return
    }
    if {[::LintAgent::json_type $parsed] ne "array"} {
        puts $json_text
        return
    }
    set items [::LintAgent::json_unwrap $parsed]
    if {[llength $items] == 0} {
        puts "no threads found"
        return
    }
    puts "recent threads:"
    set index 1
    foreach item $items {
        set tid [::LintAgent::json_string_value [::LintAgent::json_get $item "thread_id"]]
        set status [::LintAgent::json_string_value [::LintAgent::json_get $item "status"]]
        set updated [::LintAgent::json_string_value [::LintAgent::json_get $item "updated_at"]]
        set metadata [::LintAgent::json_get $item "metadata"]
        set uid [::LintAgent::json_string_value [::LintAgent::json_get $metadata "user_id"]]
        set marker " "
        if {$tid eq $current_thread} {
            set marker "*"
        }
        puts [format "%s %2d. %s  %s  %s  user=%s" $marker $index $tid $status $updated $uid]
        incr index
    }
    puts "use /resume <thread_id> to switch"
}

proc ::LintAgent::default_assistant_id {run_url} {
    variable assistant

    set body "{\"graph_id\":[::LintAgent::json_string $assistant],\"limit\":1,\"sort_by\":\"updated_at\",\"sort_order\":\"desc\"}"
    set response [::LintAgent::http_request_sync POST $run_url "/assistants/search" $body]
    set parsed [::LintAgent::json_parse $response]
    if {[::LintAgent::json_type $parsed] eq "array" && [llength [::LintAgent::json_unwrap $parsed]] > 0} {
        set first [lindex [::LintAgent::json_unwrap $parsed] 0]
        set assistant_id [::LintAgent::json_string_value [::LintAgent::json_get $first "assistant_id"]]
        if {$assistant_id ne ""} {
            return $assistant_id
        }
    }
    return $assistant
}

proc ::LintAgent::ensure_remote_thread {thread run_user run_url} {
    set body [::LintAgent::json_thread_payload $thread $run_user]
    if {[catch {::LintAgent::http_request_sync POST $run_url "/threads" $body} err]} {
        puts "warning: failed to pre-create thread metadata: $err"
    }
}

proc ::LintAgent::print_prompt_response {thread announce output_label ncode data error_text} {
    puts ""
    if {$announce} {
        puts "lint-agent request finished: prompt"
        puts "thread_id: $thread"
    }
    if {$error_text ne "" || $ncode < 200 || $ncode >= 300} {
        if {$ncode > 0} {
            puts "lint-agent failed: HTTP code=$ncode"
        } else {
            puts "lint-agent failed"
        }
        if {$error_text ne ""} {
            puts $error_text
        }
        if {$data ne ""} {
            puts $data
        }
    } else {
        set text [::LintAgent::last_ai_text $data]
        if {$output_label ne ""} {
            puts "$output_label:"
        }
        if {$text ne ""} {
            puts $text
        } else {
            puts $data
        }
    }
}

proc ::LintAgent::run_prompt_request {prompt run_user run_url run_recursion {announce 1} {output_label ""}} {
    set thread [::LintAgent::ensure_thread]
    ::LintAgent::ensure_remote_thread $thread $run_user $run_url
    set body [::LintAgent::json_run_payload $thread $prompt $run_user $run_recursion]

    if {$announce} {
        puts "lint-agent request started: prompt"
        puts "thread_id: $thread"
    }

    if {[catch {set data [::LintAgent::http_request_sync POST $run_url "/threads/$thread/runs/wait" $body]} err]} {
        ::LintAgent::print_prompt_response $thread $announce $output_label 0 "" $err
        return ""
    }
    ::LintAgent::print_prompt_response $thread $announce $output_label 200 $data ""
    return ""
}

proc ::LintAgent::run_dialog_prompt {prompt auto_approve auto_reject run_user run_url run_recursion} {
    if {$auto_approve || $auto_reject} {
        puts "warning: Tcl HTTP client does not implement interactive HITL decisions; server-side approval should be disabled."
    }
    ::LintAgent::run_prompt_request $prompt $run_user $run_url $run_recursion 0 "assistant"
    return ""
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

proc ::LintAgent::parse_threads_command {prompt} {
    set parts [split $prompt]
    set include_all 0
    set limit 10
    if {[llength $parts] >= 2} {
        if {[string tolower [lindex $parts 1]] eq "all"} {
            set include_all 1
            if {[llength $parts] >= 3} {
                set limit [lindex $parts 2]
            }
        } else {
            set limit [lindex $parts 1]
        }
    }
    if {![string is integer -strict $limit]} {
        set limit 10
    }
    if {$limit < 1} {
        set limit 1
    }
    if {$limit > 50} {
        set limit 50
    }
    return [list $include_all $limit]
}

proc ::LintAgent::sync_repl_command {slash_command run_user run_url run_recursion} {
    set thread [::LintAgent::ensure_thread]
    set lowered [string tolower [string trim $slash_command]]
    if {$lowered eq "/thread-info"} {
        puts [::LintAgent::http_request_sync GET $run_url "/threads/$thread"]
        return ""
    }
    if {$lowered eq "/state"} {
        puts [::LintAgent::http_request_sync GET $run_url "/threads/$thread/state"]
        return ""
    }
    if {$lowered eq "/graph"} {
        set assistant_id [::LintAgent::default_assistant_id $run_url]
        puts [::LintAgent::http_request_sync GET $run_url "/assistants/$assistant_id/graph"]
        return ""
    }
    if {$lowered eq "/schemas"} {
        set assistant_id [::LintAgent::default_assistant_id $run_url]
        puts [::LintAgent::http_request_sync GET $run_url "/assistants/$assistant_id/schemas"]
        return ""
    }
    if {$lowered eq "/threads" || [string match "/threads *" $lowered]} {
        lassign [::LintAgent::parse_threads_command $slash_command] include_all limit
        set body [::LintAgent::json_search_threads_payload $include_all $limit]
        set response [::LintAgent::http_request_sync POST $run_url "/threads/search" $body]
        ::LintAgent::print_thread_search $response $thread
        return ""
    }
    if {$lowered eq "/history" || [string match "/history *" $lowered]} {
        set limit [lindex [split $slash_command] 1]
        if {![string is integer -strict $limit]} {
            set limit 10
        }
        puts [::LintAgent::http_request_sync GET $run_url "/threads/$thread/history?limit=$limit"]
        return ""
    }
    if {$lowered eq "/runs" || [string match "/runs *" $lowered]} {
        set limit [lindex [split $slash_command] 1]
        if {![string is integer -strict $limit]} {
            set limit 10
        }
        puts [::LintAgent::http_request_sync GET $run_url "/threads/$thread/runs?limit=$limit"]
        return ""
    }
    if {$lowered eq "/assistant" || [string match "/assistant *" $lowered]} {
        puts [::LintAgent::http_request_sync POST $run_url "/assistants/search" "{\"graph_id\":[::LintAgent::json_string $::LintAgent::assistant],\"limit\":10}"]
        return ""
    }
    puts "unsupported command in Tcl HTTP client: $slash_command"
    return ""
}

proc ::LintAgent::dialog_help {} {
    puts "commands:"
    puts {  /new                 start a new persistent thread}
    puts {  /threads [limit]     list recent threads for the current user}
    puts {  /threads all [limit] list recent threads without user filtering}
    puts {  /resume <thread_id>  switch to an existing persistent thread}
    puts {  /thread              show the current thread_id}
    puts {  /thread-info         show current thread metadata}
    puts {  /state               show current thread state JSON}
    puts {  /history [limit]     show current thread checkpoint history JSON}
    puts {  /runs [limit]        list runs on current thread as JSON}
    puts {  /assistant           search assistants for this graph}
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
            if {[catch {::LintAgent::sync_repl_command $prompt $run_user $run_url $run_recursion} err]} {
                puts "lint-agent command failed: $err"
            }
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
    set old_thread $::LintAgent::thread_id
    set ::LintAgent::thread_id $run_thread
    ::LintAgent::run_prompt_request $prompt $run_user $run_url $run_recursion 1 ""
    if {!$switch_current_thread} {
        set ::LintAgent::thread_id $old_thread
    }
    return ""
}

proc ::LintAgent::run_repl_command {slash_command label} {
    variable url
    variable user_id
    variable recursion_limit

    return [::LintAgent::sync_repl_command $slash_command $user_id $url $recursion_limit]
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
    return [::LintAgent::run_repl_command "/assistant" "assistant"]
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
            puts "source: USERNAME/USER environment"
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
        puts "recursion_limit: $recursion_limit"
        return ""
    }
    if {[llength $args] != 1} {
        error {usage: lint-agent-recursion-limit ?number?}
    }
    set recursion_limit [lindex $args 0]
    return [::LintAgent::set_recursion_limit]
}

proc ::LintAgent::help {} {
    puts "lint-agent commands:"
    puts "  lint-agent                                  enter interactive dialogue mode"
    puts "  lint-agent \"prompt\"                       run one prompt on current thread"
    puts "  lint-agent -new \"prompt\"                  start a new thread and send prompt"
    puts "  lint-agent -thread <uuid> \"prompt\"        send prompt to a specific thread"
    puts "  lint-agent-new                            switch to a new empty thread"
    puts "  lint-agent-thread                         show current thread and user"
    puts "  lint-agent-resume <uuid>                  switch to an existing thread"
    puts "  lint-agent-threads ?all? ?limit?          list threads through HTTP API"
    puts "  lint-agent-state                          show current thread state JSON"
    puts "  lint-agent-history ?limit?                show checkpoint history JSON"
    puts "  lint-agent-runs ?limit?                   list runs on current thread as JSON"
    puts "  lint-agent-thread-info                    show thread metadata JSON"
    puts "  lint-agent-user ?user_id|-default?        show or set user_id"
    puts "  lint-agent-url ?url?                      show or set Agent Server URL"
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

puts "lint-agent command registered. Use lint-agent-help for commands."
puts "lint-agent thread_id: $::LintAgent::thread_id"
