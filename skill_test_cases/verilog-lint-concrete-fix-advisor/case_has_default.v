module case_has_default (
    input  wire [1:0] sel,
    input  wire       a,
    input  wire       b,
    output reg        y
);

always @(*) begin
    case (sel)
        2'b00: y = a;
        2'b01: y = b;
        default: y = 1'b0;
    endcase
end

endmodule
