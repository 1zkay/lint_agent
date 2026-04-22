module top_module_const_inverted_and_or_swapped (
    input  wire clk,
    input  wire rst_n,
    input  wire in1,
    input  wire in2,
    output wire out1,
    output wire out2
);

    wire flop_data_constant;
    wire flop_out;
    wire rtlc_l4;
    wire rtlc_l5;
    wire rtlc_l7;

    assign out1 = flop_out;
    assign flop_data_constant = 1'b0;

    flop_const_inverted_and_or_swapped flopInst (
        .clk   (clk),
        .rst_n (rst_n),
        .in    (rtlc_l7),
        .out   (flop_out)
    );

    // Invert flop_data_constant before feeding the logic chain.
    not rtlc_l4_inst (rtlc_l4, flop_data_constant);

    // Swap the original AND/OR roles and keep the intermediate inverter.
    or  rtlc_l5_inst (rtlc_l5, rtlc_l4, in1);
    not rtlc_l7_inst (rtlc_l7, rtlc_l5);
    and rtlc_l9_inst (out2, in2, rtlc_l7);

endmodule

module flop_const_inverted_and_or_swapped (
    input  wire clk,
    input  wire rst_n,
    input  wire in,
    output reg  out
);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out <= 1'b0;
        end else begin
            out <= in;
        end
    end

endmodule
