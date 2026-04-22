module dead_constant_case_selector (
    input  wire clk,
    input  wire a,
    input  wire b,
    input  wire c,
    output reg  y
);
localparam [1:0] MODE = 2'b01;

always @(posedge clk) begin
    case (MODE)
        2'b00: y <= a;   // unreachable: MODE is 2'b01
        2'b01: y <= b;
        default: y <= c; // unreachable for current MODE
    endcase
end

endmodule
